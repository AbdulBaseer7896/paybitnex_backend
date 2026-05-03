"""User + auth serializers."""
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.password_validation import validate_password

from myapp.Models.Auth_models import User, UserRole


class UserBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "full_name", "role", "is_profile_complete",
                  "onboarding_step"]
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

    class Meta:
        model = User
        fields = [
            "id", "email", "full_name", "role", "phone",
            "is_active", "is_profile_complete", "onboarding_step",
            "profile_picture_url",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "created_at", "updated_at", "is_profile_complete",
            "profile_picture_url",
        ]

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
            or User.objects.make_random_password()
        )
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        # expose the temp password to the creator (only in the response)
        user._plain_password = password
        return user


class AdminUpdateUserSerializer(serializers.ModelSerializer):
    """Admin edits an existing user."""
    class Meta:
        model = User
        fields = ["full_name", "phone", "role", "is_active"]

    def validate_role(self, value):
        if value not in UserRole.values:
            raise serializers.ValidationError("Invalid role.")
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
            profile = CustomerProfile.objects.get(user=self.user)
            user_payload["kyc_status"] = profile.kyc_status
            objs = profile.kyc_objections or []
            user_payload["kyc_objection_count"] = (
                len(objs) if isinstance(objs, list) else 0
            )
        except CustomerProfile.DoesNotExist:
            user_payload["kyc_status"] = None
            user_payload["kyc_objection_count"] = 0
        data["user"] = user_payload
        return data
