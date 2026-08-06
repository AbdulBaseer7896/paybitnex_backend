"""User + auth serializers."""
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.password_validation import validate_password
from django.utils.crypto import get_random_string

from myapp.Models.Auth_models import User, UserRole


class UserBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "full_name", "role", "is_profile_complete",
                  "onboarding_step", "email_verified", "verification_deadline"]
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    """Read serializer — includes a profile-picture URL if present.

    NOTE: the `features` map is NOT exposed here. It's injected by
    the views that return this payload (MeView, UserAdminViewSet) so
    async views can await the async helper and sync views can call
    the sync one. Putting an ORM-touching SerializerMethodField here
    caused SynchronousOnlyOperation when MeView (async) serialised a
    user who had any CustomerFeatureAccess rows.
    """
    profile_picture_url = serializers.SerializerMethodField()
    # Whether the customer has set a "My Payments" PIN (never the PIN itself).
    payments_pin_set = serializers.SerializerMethodField()
    # Vendor-portal flags. A vendor is a distinct KIND of account from a
    # trading customer, so the admin UI needs to tell them apart (filter
    # the user list, hide customer-only feature toggles, etc.).
    is_vendor = serializers.SerializerMethodField()
    vendor_name = serializers.SerializerMethodField()
    bank_accounts = serializers.SerializerMethodField()
    merchant_accounts = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "email", "full_name", "role", "phone",
            "is_active", "email_verified", "verification_deadline",
            "is_profile_complete", "onboarding_step",
            "profile_picture_url",
            "payments_pin_set",
            "is_vendor", "vendor_name",
            "bank_accounts", "merchant_accounts",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "created_at", "updated_at", "is_profile_complete",
            "email_verified", "verification_deadline",
            "profile_picture_url", "payments_pin_set",
            "is_vendor", "vendor_name",
            "bank_accounts", "merchant_accounts",
        ]

    def get_payments_pin_set(self, obj):
        return bool(getattr(obj, "payments_pin_hash", ""))

    def _vendor(self, obj):
        """Resolve the linked Vendor, tolerating a missing migration.

        Uses the `vendor_profile` reverse accessor, which the viewset
        select_related()s — so listing users stays a single query rather
        than one extra per row.
        """
        try:
            v = getattr(obj, "vendor_profile", None)
            if v is not None and v.portal_enabled and v.is_active:
                return v
        except Exception:
            pass
        return None

    def get_is_vendor(self, obj):
        return self._vendor(obj) is not None

    def get_vendor_name(self, obj):
        v = self._vendor(obj)
        return v.name if v else ""

    def _can_view_accounts(self, obj):
        request = self.context.get("request")
        if not request or not getattr(request.user, "is_authenticated", False):
            return False
        return request.user.pk == obj.pk or request.user.role in (
            "admin", "accountant",
        )

    def get_bank_accounts(self, obj):
        if not self._can_view_accounts(obj):
            return []
        from myapp.serializers.Banking_serializers import (
            CustomerBankAccountSerializer,
        )
        return CustomerBankAccountSerializer(
            obj.bank_accounts.all(), many=True, context=self.context,
        ).data

    def get_merchant_accounts(self, obj):
        if not self._can_view_accounts(obj):
            return []
        from myapp.serializers.Banking_serializers import (
            CustomerMerchantAccountSerializer,
        )
        return CustomerMerchantAccountSerializer(
            obj.merchant_accounts.all(), many=True, context=self.context,
        ).data

    def get_profile_picture_url(self, obj):
        # Explicit profile picture takes priority.
        if getattr(obj, "profile_picture", None):
            try:
                return obj.profile_picture.url
            except Exception:
                pass
        # Fallback: for customers, use their selfie from the KYC profile.
        try:
            profile = obj.profile
            if profile and profile.selfie:
                return profile.selfie.url
        except Exception:
            return None
        return None


class CustomerAccountDetailSerializer(serializers.ModelSerializer):
    """Everything the staff "Customer details" popup shows, in one payload.

    Customers already give us all of this during onboarding, but it lives
    across the user row, the KYC profile and the two account tables — so
    staff kept re-asking customers for details we already had. Flattening
    it here lets every screen open the same popup with a single request.

    Read-only by design: edits still go through the banking / profile
    endpoints so they stay audit-logged.
    """
    bank_accounts = serializers.SerializerMethodField()
    merchant_accounts = serializers.SerializerMethodField()
    cnic_number = serializers.SerializerMethodField()
    kyc_status = serializers.SerializerMethodField()
    address = serializers.SerializerMethodField()
    city = serializers.SerializerMethodField()
    profile_full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "email", "full_name", "phone",
            "is_active", "is_profile_complete",
            "cnic_number", "kyc_status", "address", "city",
            "profile_full_name",
            "bank_accounts", "merchant_accounts",
            "created_at",
        ]
        read_only_fields = fields

    def get_bank_accounts(self, obj):
        from myapp.serializers.Banking_serializers import (
            CustomerBankAccountSerializer,
        )
        return CustomerBankAccountSerializer(
            obj.bank_accounts.all(), many=True, context=self.context,
        ).data

    def get_merchant_accounts(self, obj):
        from myapp.serializers.Banking_serializers import (
            CustomerMerchantAccountSerializer,
        )
        return CustomerMerchantAccountSerializer(
            obj.merchant_accounts.all(), many=True, context=self.context,
        ).data

    # `profile` is a reverse OneToOne — absent for customers who signed up
    # but never finished KYC. Accessing it then raises RelatedObjectDoesNotExist
    # (an AttributeError subclass), which getattr's default swallows.
    def _profile(self, obj):
        return getattr(obj, "profile", None)

    def get_cnic_number(self, obj):
        p = self._profile(obj)
        return p.cnic_number if p else ""

    def get_kyc_status(self, obj):
        p = self._profile(obj)
        return p.kyc_status if p else None

    def get_address(self, obj):
        p = self._profile(obj)
        return p.address if p else ""

    def get_city(self, obj):
        p = self._profile(obj)
        return p.city if p else ""

    # The KYC profile carries the legally-verified name, which can differ
    # from the display name on the user row — reviewers need to see both.
    def get_profile_full_name(self, obj):
        p = self._profile(obj)
        return p.full_name if p else ""


class AdminCreateUserSerializer(serializers.ModelSerializer):
    """Admin creates a new user (any role). Temporary password returned."""
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "id", "email", "full_name", "role", "phone",
            "password", "is_active",
        ]
        read_only_fields = ["id"]

    def validate_role(self, value):
        if value not in UserRole.values:
            raise serializers.ValidationError("Invalid role.")
        return value

    # Manual email-uniqueness check so this stays safe inside async views.
    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "A user with that email already exists."
            )
        return value

    def create(self, validated_data):
        password = (
            validated_data.pop("password", None)
            or get_random_string(12)
        )
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        # expose the temp password to the creator (only in the response)
        user._plain_password = password
        return user


class AdminUpdateUserSerializer(serializers.ModelSerializer):
    """Admin / accountant edits an existing user.

    `email` is now editable so staff can correct a mistyped email after a
    user/customer account was created (e.g. xyz@gmail.com → abc@gmail.com).
    Uniqueness is checked manually (case-insensitive) so this stays safe
    inside async views and gives a clear field-level error.
    """
    email = serializers.EmailField(required=False)

    class Meta:
        model = User
        fields = ["email", "full_name", "phone", "role", "is_active"]

    def validate_role(self, value):
        if value not in UserRole.values:
            raise serializers.ValidationError("Invalid role.")
        return value

    def validate_email(self, value):
        if not value:
            return value
        value = value.strip()
        qs = User.objects.filter(email__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                "A user with that email already exists."
            )
        return value


class AdminResetPasswordSerializer(serializers.Serializer):
    """Admin forces a new password for a user.

    Either supply `new_password` or leave blank for one to be generated.
    """
    new_password = serializers.CharField(required=False, allow_blank=True)

    def validate_new_password(self, value):
        if value:
            validate_password(value)
        return value


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(required=True)
    new_password = serializers.CharField(required=True)

    def validate_new_password(self, value):
        validate_password(value)
        return value


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Include role + profile-complete flag in the token payload."""
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["email"] = user.email
        token["is_profile_complete"] = user.is_profile_complete
        token["onboarding_step"] = user.onboarding_step
        return token

    def validate(self, attrs):
        # Before calling super() (which raises a generic AuthenticationFailed),
        # check if the email exists so we can give specific error messages:
        #   - Email not found → "This account does not exist."
        #   - Email found but wrong password → "Your password is incorrect."
        email = attrs.get("email", "").lower()
        try:
            user_obj = User.objects.get(email__iexact=email)
            # Email exists — now check if the password is wrong
            if not user_obj.check_password(attrs.get("password", "")):
                from rest_framework_simplejwt.exceptions import AuthenticationFailed
                raise AuthenticationFailed(
                    "Your password is incorrect. Please try again.",
                    code="incorrect_password",
                )
            if not user_obj.is_active:
                from rest_framework_simplejwt.exceptions import AuthenticationFailed
                if not getattr(user_obj, "email_verified", True) and user_obj.role != UserRole.ADMIN:
                    raise AuthenticationFailed(
                        "Please verify your email to continue.",
                        code="EMAIL_UNVERIFIED",
                    )
                raise AuthenticationFailed(
                    "This account has been deactivated. Please contact support.",
                    code="account_deactivated",
                )
        except User.DoesNotExist:
            from rest_framework_simplejwt.exceptions import AuthenticationFailed
            raise AuthenticationFailed(
                "This account does not exist. Please check your email or sign up.",
                code="account_not_found",
            )

        data = super().validate(attrs)
        # Login endpoint is sync (TokenObtainPairView), so the sync
        # helper is the correct one here. This keeps the frontend's
        # `user.features` populated on the very first request after
        # login, before `/auth/me/` is called.
        from myapp.Utils.features import user_feature_map
        user_payload = UserBriefSerializer(self.user).data
        user_payload["features"] = user_feature_map(self.user)
        # Include kyc_status so the sidebar can render the "Onboarding"
        # nav item from the very first paint after login (no extra
        # round trip needed). Staff users have no profile row, so the
        # field stays null and the sidebar item simply doesn't show.
        from myapp.Models.Profile_models import CustomerProfile
        try:
            # Use .only() so this query fetches ONLY the two fields we
            # need — not the full row. This means a missing migration
            # (e.g. a new column that hasn't been applied to prod yet)
            # cannot break login by hitting an UndefinedColumn error.
            profile = (
                CustomerProfile.objects
                .only("kyc_status", "kyc_objections")
                .get(user=self.user)
            )
            user_payload["kyc_status"] = profile.kyc_status
            objs = profile.kyc_objections or []
            user_payload["kyc_objection_count"] = (
                len(objs) if isinstance(objs, list) else 0
            )
        except CustomerProfile.DoesNotExist:
            user_payload["kyc_status"] = None
            user_payload["kyc_objection_count"] = 0

        # Vendor-portal context, so the router can send a vendor straight
        # to their portal on the first paint after login rather than
        # flashing the customer dashboard first.
        #
        # Same .only()/broad-except discipline as the KYC block above: if
        # migration 0053 has not been applied, touching the new portal_*
        # columns would raise UndefinedColumn and break login for EVERY
        # user. Degrading to "not a vendor" is the safe failure.
        user_payload["is_vendor"] = False
        user_payload["vendor"] = None
        try:
            from myapp.Models.InternalTx_models import Vendor
            vendor = (
                Vendor.objects
                .only("id", "name", "portal_enabled", "is_active", "portal_user")
                .get(portal_user=self.user, portal_enabled=True, is_active=True)
            )
            user_payload["is_vendor"] = True
            user_payload["vendor"] = {"id": str(vendor.id), "name": vendor.name}
        except Exception:
            pass

        data["user"] = user_payload
        return data
