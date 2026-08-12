"""
Account views:
  - Admin CRUD on users (customers, accountants, admins) with reset
    password and toggle-active actions.
  - Customer profile CRUD.
  - Customer score endpoint.
  - Accountant/admin KYC review.
  - Admin "onboarding review" — list recent customers to verify.
"""
import logging

from adrf.views import APIView as AsyncAPIView
from django.utils import timezone
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from django.core.exceptions import ValidationError
from myapp.Models.Auth_models import User, UserRole
from myapp.Models.Profile_models import CustomerProfile
from myapp.Models.Audit_models import AuditLog
from myapp.serializers.User_serializers import (
    UserSerializer, AdminCreateUserSerializer,
    AdminUpdateUserSerializer, AdminResetPasswordSerializer,
    CustomerAccountDetailSerializer,
)
from myapp.serializers.Profile_serializers import (
    CustomerProfileSerializer, KYCReviewSerializer,
    KYCRaiseObjectionsSerializer,
)
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant
from myapp.Utils.async_helpers import async_is_valid, async_save
from myapp.Utils.customer_scoring import compute_score
from myapp.Utils.email_tasks import send_email_async
from myapp.Utils.staff_alerts import notify_staff, anotify_staff

log = logging.getLogger(__name__)


def _kyc_alert_context(profile, user, changed=None):
    """Body fields shared by the two staff-facing KYC alerts."""
    return {
        "customer_name":  profile.full_name or user.full_name or user.email,
        "customer_email": user.email,
        "phone":          profile.phone or "",
        # Only set on a resubmission — tells the reviewer what to re-check
        # instead of making them diff the whole profile by eye.
        "changed":        [f.replace("_", " ") for f in (changed or [])],
    }


# =====================================================================
#  ADMIN USER CRUD
# =====================================================================
class UserAdminViewSet(viewsets.ModelViewSet):
    """Admin (+ accountant for edits): list / create / update / delete users.

    Extra actions:
      POST /users/{id}/reset_password/   → force-reset password
      POST /users/{id}/toggle_active/    → flip is_active

    Permissions:
      - Admins have full access (create / update / delete / reset / toggle).
      - Accountants may LIST, RETRIEVE and UPDATE users (so they can correct
        a customer's email/name), but cannot create, delete, reset passwords
        or toggle active state. This is enforced in `get_permissions`.
    """
    permission_classes = [IsAuthenticated, IsAdmin]
    # select_related pulls the linked Vendor in the SAME query, so
    # UserSerializer.is_vendor doesn't fire one extra SELECT per user row.
    queryset = (
        User.objects.all()
        .select_related("vendor_profile")
        .prefetch_related(
            "bank_accounts__bank",
            "merchant_accounts__bank",
        )
        .order_by("-created_at")
    )
    serializer_class = UserSerializer
    filterset_fields = ["role", "is_active", "is_profile_complete"]
    search_fields = ["email", "full_name", "phone"]

    # Actions an accountant is allowed to perform (everything else is admin-only).
    _ACCOUNTANT_ALLOWED = {"list", "retrieve", "update", "partial_update"}

    def get_permissions(self):
        """Accountants get read + edit; admins get everything."""
        if self.action in self._ACCOUNTANT_ALLOWED:
            return [IsAuthenticated(), IsAdminOrAccountant()]
        return [IsAuthenticated(), IsAdmin()]

    def get_queryset(self):
        """List users, with a free-text `q` search over email, name, phone.

        The admin Users page sends `?q=` (not DRF's default `search`), so we
        resolve it explicitly here rather than relying on SearchFilter. Each
        whitespace-separated token must match at least one field, so
        "john gmail" narrows to rows matching both.
        """
        qs = super().get_queryset()

        # `user_type` filter — vendors are a distinct KIND of account from
        # trading customers even though both carry role='customer', so the
        # admin Users page can narrow to one or the other.
        #   customer -> role=customer WITHOUT live vendor access
        #   vendor   -> role=customer WITH live vendor access
        user_type = (self.request.query_params.get("user_type") or "").strip().lower()
        if user_type == "vendor":
            qs = qs.filter(
                vendor_profile__isnull=False,
                vendor_profile__portal_enabled=True,
                vendor_profile__is_active=True,
            )
        elif user_type == "customer":
            qs = qs.filter(role=UserRole.CUSTOMER).exclude(
                vendor_profile__isnull=False,
                vendor_profile__portal_enabled=True,
                vendor_profile__is_active=True,
            )

        q = (self.request.query_params.get("q") or "").strip()
        if q:
            for token in q.split():
                qs = qs.filter(
                    Q(email__icontains=token) |
                    Q(full_name__icontains=token) |
                    Q(phone__icontains=token)
                )
        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return AdminCreateUserSerializer
        if self.action in ("update", "partial_update"):
            return AdminUpdateUserSerializer
        return UserSerializer

    def perform_create(self, serializer):
        user = serializer.save(created_by=self.request.user)
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_CREATE, target=user,
            description=f"Created {user.role} account for {user.email}",
        )
        # Auto-assign default payment methods for new customers
        if getattr(user, 'role', None) == 'customer':
            try:
                from myapp.Utils.auto_assign_payment_methods import assign_defaults_to_user
                assign_defaults_to_user(user, granted_by=self.request.user)
            except Exception:
                pass  # Never block user creation

        # Without this the account is invisible to its owner: the temporary
        # password is only returned in the API response for the admin to
        # relay by hand, so nothing tells the customer they have a login.
        temp_password = getattr(user, "_plain_password", None)
        if user.email and temp_password:
            try:
                send_email_async(
                    to=[user.email],
                    subject="Your PaidiX account is ready",
                    template="auth/account_created",
                    context={
                        "name": user.full_name or "",
                        "email": user.email,
                        "temporary_password": temp_password,
                        "role": user.get_role_display(),
                        "is_customer": user.role == UserRole.CUSTOMER,
                    },
                )
            except Exception:
                log.exception("account-created email failed for %s", user.email)

    def create(self, request, *args, **kwargs):
        s = self.get_serializer(data=request.data)
        s.is_valid(raise_exception=True)
        self.perform_create(s)
        user = s.instance
        return Response(
            {
                **UserSerializer(user, context={"request": request}).data,
                "temporary_password": getattr(user, "_plain_password", None),
            },
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        before = {
            "email": instance.email,
            "full_name": instance.full_name, "role": instance.role,
            "is_active": instance.is_active, "phone": instance.phone,
        }
        old_email = instance.email
        s = self.get_serializer(instance, data=request.data, partial=partial)
        s.is_valid(raise_exception=True)
        self.perform_update(s)
        instance.refresh_from_db()
        email_changed = old_email and instance.email and old_email != instance.email
        desc = (
            f"Admin changed email {old_email} → {instance.email}"
            if email_changed else
            f"Admin updated user {instance.email}"
        )
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_UPDATE, target=instance,
            description=desc,
            before=before,
            after={
                "email": instance.email,
                "full_name": instance.full_name, "role": instance.role,
                "is_active": instance.is_active, "phone": instance.phone,
            },
        )
        return Response(
            UserSerializer(instance, context={"request": request}).data
        )

    def destroy(self, request, *args, **kwargs):
        u = self.get_object()
        if u == request.user:
            return Response(
                {"detail": "Cannot delete your own account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        email = u.email
        u.delete()
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_DELETE,
            target_label=email,
            description=f"Deleted user {email}",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    # NOTE: url_path uses dashes ("reset-password") to match the REST-API
    # convention the frontend expects. Without this, DRF auto-generates the
    # url_path from the method name ("reset_password") which produces a 404
    # from the /reset-password/ URL the UI calls.
    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        user = self.get_object()
        s = AdminResetPasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        new_pw = (
            s.validated_data.get("new_password")
            or User.objects.make_random_password()
        )
        user.set_password(new_pw)
        user.save(update_fields=["password", "updated_at"])
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_PASSWORD_RESET,
            target=user,
            description=f"Admin reset password for {user.email}",
        )
        return Response({
            "detail": "Password reset.",
            "temporary_password": new_pw,
        })

    @action(detail=True, methods=["post"], url_path="toggle-active")
    def toggle_active(self, request, pk=None):
        user = self.get_object()
        if user == request.user:
            return Response(
                {"detail": "Cannot deactivate your own account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        before = user.is_active
        user.is_active = not user.is_active
        user.save(update_fields=["is_active", "updated_at"])
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_TOGGLE_ACTIVE, target=user,
            description=f"{'Activated' if user.is_active else 'Deactivated'} {user.email}",
            before={"is_active": before}, after={"is_active": user.is_active},
        )
        return Response(
            UserSerializer(user, context={"request": request}).data
        )


import base64
from rest_framework.views import APIView


class ProcessImageView(APIView):
    """
    POST /api/v1/accounts/process-img/
    Protected endpoint to process CNIC Front, CNIC Back, and Selfie/Profile images
    using the complete CV pipeline (edge detection, perspective crop, glare removal,
    CLAHE enhancement, OCR rotation) WITHOUT adding watermarks.
    Returns base64 encoded data URLs for instant frontend preview.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from myapp.Utils.image_processor import process_uploaded_image

        files_to_process = {
            "cnic_front": request.FILES.get("cnic_front"),
            "cnic_back": request.FILES.get("cnic_back"),
            "selfie": request.FILES.get("selfie") or request.FILES.get("profile_pic"),
        }

        if not any(files_to_process.values()):
            return Response(
                {"detail": "At least one image ('cnic_front', 'cnic_back', or 'selfie') is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = {}
        for key, uploaded_file in files_to_process.items():
            if uploaded_file:
                processed = process_uploaded_image(
                    uploaded_file,
                    watermark_path=None,
                    process_cv=(key != "selfie"),
                )
                img_bytes = processed.read()
                b64_str = base64.b64encode(img_bytes).decode("utf-8")
                result[key] = f"data:image/webp;base64,{b64_str}"
                result[f"{key}_name"] = processed.name

        return Response(result, status=status.HTTP_200_OK)


# =====================================================================
#  CUSTOMER PROFILE
# =====================================================================
class CustomerProfileView(AsyncAPIView):
    permission_classes = [IsAuthenticated]

    async def get(self, request, user_id=None):
        """Own profile, or another user's when called by staff.

        `user_id` is only honoured for admin/accountant — a customer
        passing someone else's id still gets their own row, never a
        different customer's KYC data.

        GET only: POST/PATCH below remain self-service, so this read path
        can't be used to modify another user's profile.
        """
        target = request.user
        if user_id and request.user.role in (UserRole.ADMIN, UserRole.ACCOUNTANT):
            try:
                target = await User.objects.aget(pk=user_id)
            except (User.DoesNotExist, ValueError, ValidationError):
                return Response({"detail": "User not found."},
                                status=status.HTTP_404_NOT_FOUND)
        try:
            profile = await CustomerProfile.objects.select_related(
                "user", "kyc_reviewed_by"
            ).defer(
                "kyc_last_resubmit_at", "kyc_last_resubmit_changes"
            ).aget(
                user=target
            )
        except CustomerProfile.DoesNotExist:
            return Response({"detail": "Profile not set up."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(
            CustomerProfileSerializer(profile, context={"request": request}).data
        )

    async def post(self, request, user_id=None):
        # The staff read-route (users/<uuid>/profile/) shares this view.
        # Writes must stay strictly self-service, so reject any attempt to
        # create a profile *for* another user.
        if user_id:
            return Response(
                {"detail": "Profiles can only be created by their owner."},
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )

        try:
            profile = await CustomerProfile.objects.select_related(
                "user", "kyc_reviewed_by"
            ).aget(user=request.user)

            if profile.is_locked:
                return Response(
                    {"detail": "Profile is locked after KYC approval and cannot be edited."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            s = CustomerProfileSerializer(
                profile, data=request.data, partial=True,
                context={"request": request},
            )

            await async_is_valid(s, raise_exception=True)
            profile = await async_save(s)
        except CustomerProfile.DoesNotExist:
            s = CustomerProfileSerializer(
                data=request.data, context={"request": request},
            )
            await async_is_valid(s, raise_exception=True)
            profile = await async_save(s, user=request.user)

        request.user.is_profile_complete = True
        request.user.full_name = profile.full_name
        request.user.phone = profile.phone
        request.user.onboarding_step = 4
        await request.user.asave(
            update_fields=["is_profile_complete", "full_name", "phone",
                           "onboarding_step"],
        )

        # KYC gates payment submission entirely (see Transaction_views.create),
        # so an unreviewed profile silently blocks the customer from doing
        # anything at all. Only alert while it's actually awaiting a decision.
        if profile.kyc_status in (
            CustomerProfile.KYC_PENDING, CustomerProfile.KYC_RESUBMITTED,
        ):
            await anotify_staff(
                subject=f"KYC awaiting review — {profile.full_name or request.user.email}",
                template="staff/kyc_pending",
                context=_kyc_alert_context(profile, request.user),
                path="/kyc",
                reply_to=[request.user.email] if request.user.email else None,
            )

        return Response(
            CustomerProfileSerializer(profile, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )

    async def patch(self, request, user_id=None):
        # Same rule as post(): writes are self-service only. See above.
        if user_id:
            return Response(
                {"detail": "Profiles can only be edited by their owner."},
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )
        try:
            profile = await CustomerProfile.objects.select_related(
                "user", "kyc_reviewed_by"
            ).defer(
                "kyc_last_resubmit_at", "kyc_last_resubmit_changes"
            ).aget(user=request.user)
        except CustomerProfile.DoesNotExist:
            return Response({"detail": "Profile not set up."},
                            status=status.HTTP_404_NOT_FOUND)

        # Locked profiles (approved KYC) cannot be edited at all.
        if profile.is_locked:
            return Response(
                {"detail": "Profile is locked after KYC approval and cannot be edited."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # ── Capture pre-PATCH state for the resubmission diff ──
        # We snapshot the relevant fields BEFORE the serializer
        # saves the new values, then compare each field to detect
        # what the customer actually changed. The reviewer needs
        # this on the next round so they can spot-check just the
        # updated fields rather than re-reviewing everything.
        # File fields are detected by checking whether the request
        # included a multipart upload for that key — that's the
        # only reliable signal for "the customer replaced this
        # photo" since the model field always points to *some*
        # object regardless.
        was_resubmission = profile.kyc_status in (
            CustomerProfile.KYC_OBJECTIONS,
            CustomerProfile.KYC_REJECTED,
        )
        pre_state = {
            "full_name":   profile.full_name or "",
            "phone":       profile.phone or "",
            "cnic_number": profile.cnic_number or "",
            "address":     profile.address or "",
            "city":        profile.city or "",
        }

        s = CustomerProfileSerializer(
            profile, data=request.data, partial=True,
            context={"request": request},
        )
        await async_is_valid(s, raise_exception=True)
        profile = await async_save(s)

        # If the customer was responding to objections, flip status to RESUBMITTED.
        if was_resubmission:
            # Compute the diff between pre- and post-PATCH state
            # for the simple text fields. For files, look at the
            # raw request — if a multipart field is present, the
            # customer uploaded a new photo regardless of whether
            # its filename matches the old one.
            changed = []
            for field, before in pre_state.items():
                after = getattr(profile, field, None) or ""
                if before != after:
                    changed.append(field)
            for file_field in ("selfie", "cnic_front", "cnic_back"):
                if file_field in request.FILES:
                    changed.append(file_field)

            profile.kyc_status = CustomerProfile.KYC_RESUBMITTED
            profile.kyc_last_resubmit_at = timezone.now()
            profile.kyc_last_resubmit_changes = changed
            try:
                await profile.asave(update_fields=[
                    "kyc_status", "kyc_last_resubmit_at",
                    "kyc_last_resubmit_changes", "updated_at",
                ])
            except Exception:
                # Migration 0033 columns may not exist yet — fall back
                # to saving only the status change.
                await profile.asave(update_fields=["kyc_status", "updated_at"])
            await AuditLog.arecord(
                user=request.user, action=AuditLog.ACTION_UPDATE, target=profile,
                description=(
                    f"Customer resubmitted KYC for {profile.full_name} "
                    f"(round {profile.kyc_objection_round}) — "
                    f"changed: {', '.join(changed) or 'no fields detected'}"
                ),
                after={
                    "kyc_status": profile.kyc_status,
                    "kyc_last_resubmit_changes": changed,
                },
            )
            await anotify_staff(
                subject=(
                    f"KYC resubmitted — {profile.full_name or request.user.email}"
                ),
                template="staff/kyc_pending",
                context=_kyc_alert_context(profile, request.user, changed=changed),
                path="/kyc",
                reply_to=[request.user.email] if request.user.email else None,
            )

        return Response(
            CustomerProfileSerializer(profile, context={"request": request}).data
        )


# =====================================================================
#  CUSTOMER SCORE
# =====================================================================
class CustomerScoreView(AsyncAPIView):
    """Returns auto-computed score for the current user (or any user if admin)."""
    permission_classes = [IsAuthenticated]

    async def get(self, request, user_id=None):
        from asgiref.sync import sync_to_async

        if user_id and request.user.role in (UserRole.ADMIN, UserRole.ACCOUNTANT):
            try:
                target = await User.objects.aget(pk=user_id)
            except (User.DoesNotExist, ValueError, ValidationError):
                return Response({"detail": "Not found."},
                                status=status.HTTP_404_NOT_FOUND)
        else:
            target = request.user

        data = await sync_to_async(compute_score)(target)
        data["user_id"] = str(target.pk)
        data["user_email"] = target.email
        return Response(data)


# =====================================================================
#  KYC REVIEW
# =====================================================================
class KYCReviewView(AsyncAPIView):
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]

    async def post(self, request, profile_id):
        try:
            profile = await CustomerProfile.objects.select_related(
                "user", "kyc_reviewed_by"
            ).defer(
                "kyc_last_resubmit_at", "kyc_last_resubmit_changes"
            ).aget(pk=profile_id)
        except CustomerProfile.DoesNotExist:
            return Response({"detail": "Not found."},
                            status=status.HTTP_404_NOT_FOUND)

        s = KYCReviewSerializer(data=request.data)
        await async_is_valid(s, raise_exception=True)

        validated = s.validated_data or {}
        new_status = validated.get("status")
        if new_status is None:
            raise ValidationError({"status": ["This field is required."]})

        before = profile.kyc_status
        profile.kyc_status = new_status
        profile.kyc_notes = validated.get("notes", "")
        profile.kyc_reviewed_by = request.user
        profile.kyc_reviewed_at = timezone.now()

        update_fields = [
            "kyc_status", "kyc_notes", "kyc_reviewed_by", "kyc_reviewed_at",
        ]

        # On approval: lock the profile, clear any outstanding objections.
        if new_status == CustomerProfile.KYC_APPROVED:
            profile.kyc_approved_at = timezone.now()
            profile.kyc_objections = []
            # Clear the resubmit diff — once approved, the
            # "what changed last round" highlights are no longer
            # relevant. Keeps the data tidy for any subsequent
            # admin views.
            profile.kyc_last_resubmit_changes = []
            profile.kyc_last_resubmit_at = None
            update_fields += [
                "kyc_approved_at", "kyc_objections",
                "kyc_last_resubmit_changes", "kyc_last_resubmit_at",
            ]

        try:
            await profile.asave(update_fields=update_fields)
        except Exception:
            # Migration 0033 columns missing — retry without them.
            safe_fields = [f for f in update_fields
                           if f not in ("kyc_last_resubmit_at", "kyc_last_resubmit_changes")]
            await profile.asave(update_fields=safe_fields)
        await AuditLog.arecord(
            user=request.user, action=AuditLog.ACTION_KYC_REVIEW, target=profile,
            description=f"KYC {before} → {profile.kyc_status} for {profile.full_name}",
            before={"kyc_status": before},
            after={"kyc_status": profile.kyc_status},
        )

        # Customer-facing email on approval ONLY (objections are handled
        # by the separate KYCRaiseObjectionsView below). No admin/accountant
        # addresses are in this email — customer-only.
        if (new_status == CustomerProfile.KYC_APPROVED
                and before != CustomerProfile.KYC_APPROVED):
            try:
                customer_email = profile.user.email
                customer_name = profile.user.full_name or profile.full_name or ""
                send_email_async(
                    to=[customer_email],
                    subject="Your PaidiX account has been verified",
                    template="kyc/approved",
                    context={"name": customer_name},
                )
            except Exception:
                # Email failure must not block the verification response —
                # but log it, or a broken mail config is indistinguishable
                # from "no email was supposed to go out".
                log.exception(
                    "KYC approval email failed for profile=%s", profile_id,
                )

        # Rejection is a hard stop for the customer: KYC gates payment
        # submission, so without this they're blocked with no explanation
        # and no reason to check the portal.
        if (new_status == CustomerProfile.KYC_REJECTED
                and before != CustomerProfile.KYC_REJECTED):
            try:
                send_email_async(
                    to=[profile.user.email],
                    subject="Update on your PaidiX verification",
                    template="kyc/rejected",
                    context={
                        "name": profile.user.full_name or profile.full_name or "",
                        "reason": profile.kyc_notes or "",
                    },
                )
            except Exception:
                log.exception(
                    "KYC rejection email failed for profile=%s", profile_id,
                )

        return Response(CustomerProfileSerializer(profile).data)


class KYCRaiseObjectionsView(AsyncAPIView):
    """
    POST /accounts/kyc/<profile_id>/objections/

    Admin/accountant raises one or more objections on a KYC submission.
    The customer then receives these in their profile view and can edit
    specific fields to address them, which flips status to RESUBMITTED.
    """
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]

    async def post(self, request, profile_id):
        try:
            profile = await CustomerProfile.objects.select_related(
                "user", "kyc_reviewed_by"
            ).defer(
                "kyc_last_resubmit_at", "kyc_last_resubmit_changes"
            ).aget(pk=profile_id)
        except CustomerProfile.DoesNotExist:
            return Response({"detail": "Not found."},
                            status=status.HTTP_404_NOT_FOUND)

        if profile.kyc_status == CustomerProfile.KYC_APPROVED:
            return Response(
                {"detail": "Cannot raise objections — profile is already approved."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        s = KYCRaiseObjectionsSerializer(data=request.data)
        await async_is_valid(s, raise_exception=True)

        validated = s.validated_data or {}
        objections = validated.get("objections") or []
        notes = validated.get("notes", profile.kyc_notes or "")

        now = timezone.now()
        raised_by_email = getattr(request.user, "email", "")
        new_items = [
            {
                "field": item["field"],
                "message": item["message"],
                "raised_at": now.isoformat(),
                "raised_by": raised_by_email,
            }
            for item in objections
        ]

        before_status = profile.kyc_status
        profile.kyc_objections = new_items
        profile.kyc_objection_round = (profile.kyc_objection_round or 0) + 1
        profile.kyc_status = CustomerProfile.KYC_OBJECTIONS
        profile.kyc_notes = notes
        profile.kyc_reviewed_by = request.user
        profile.kyc_reviewed_at = now
        # Reset the resubmit diff — the customer is starting a new
        # round of objections, so any previously-recorded "what
        # changed last time" is now stale and would be misleading
        # if the customer never updates anything before the next
        # admin review.
        # Guard: only include resubmit fields in update_fields if columns exist.
        obj_update_fields = [
            "kyc_objections", "kyc_objection_round", "kyc_status",
            "kyc_notes", "kyc_reviewed_by", "kyc_reviewed_at",
        ]
        profile.kyc_last_resubmit_changes = []
        profile.kyc_last_resubmit_at = None
        obj_update_fields += ["kyc_last_resubmit_changes", "kyc_last_resubmit_at"]
        try:
            await profile.asave(update_fields=obj_update_fields)
        except Exception:
            # Migration 0033 columns missing — retry without them.
            safe_fields = [f for f in obj_update_fields
                           if f not in ("kyc_last_resubmit_at", "kyc_last_resubmit_changes")]
            await profile.asave(update_fields=safe_fields)

        await AuditLog.arecord(
            user=request.user, action=AuditLog.ACTION_KYC_REVIEW, target=profile,
            description=(
                f"KYC objections raised (round {profile.kyc_objection_round}) "
                f"for {profile.full_name}: {len(new_items)} item(s)"
            ),
            before={"kyc_status": before_status},
            after={
                "kyc_status": profile.kyc_status,
                "objections": new_items,
            },
        )

        # Send a notification email to the customer with the list of
        # objections and a link to update their profile. Previously this
        # was handled only by an on-screen message inside the dashboard,
        # which meant a user who didn't log in for a few days had no way
        # of knowing their submission had been objected to. Best-effort:
        # if email fails for any reason we still return success so the
        # admin's action is recorded.
        try:
            # Map field codes to user-facing labels so the email reads
            # naturally ("Selfie" instead of "selfie", "CNIC — front"
            # instead of "cnic_front"). Keep this list in sync with the
            # frontend's OBJECTION_FIELDS in src/pages/admin/AdminOnboarding.jsx.
            FIELD_LABELS = {
                "selfie":      "Selfie",
                "cnic_front":  "CNIC — front",
                "cnic_back":   "CNIC — back",
                "cnic_number": "CNIC number",
                "full_name":   "Full name",
                "phone":       "Phone",
                "address":     "Address",
                "city":        "City",
                "bank":        "Bank",
                "general":     "General",
            }
            user = profile.user
            if user and user.email:
                template_objections = [
                    {
                        "field_label": FIELD_LABELS.get(item["field"], item["field"]),
                        "message": item["message"],
                    }
                    for item in new_items
                ]
                send_email_async(
                    to=[user.email],
                    subject="Action required: PaidiX profile objections",
                    template="kyc/objection_raised",
                    context={
                        "name": user.full_name or profile.full_name or "",
                        "objection_count": len(new_items),
                        "objections": template_objections,
                        "notes": s.validated_data.get("notes", ""),
                    },
                )
        except Exception:
            # Log via stdlib but don't propagate — admin already has a
            # successful response by the time we get here.
            import logging
            logging.getLogger(__name__).exception(
                "Failed to enqueue KYC objection email for profile %s", profile.id,
            )

        return Response(CustomerProfileSerializer(profile).data)


class PendingKYCListView(AsyncAPIView):
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]

    async def get(self, request):
        qs = CustomerProfile.objects.filter(
            kyc_status__in=[
                CustomerProfile.KYC_PENDING,
                CustomerProfile.KYC_RESUBMITTED,
            ],
        ).select_related("user", "kyc_reviewed_by").defer(
            "kyc_last_resubmit_at", "kyc_last_resubmit_changes"
        ).order_by("created_at")
        results = [
            CustomerProfileSerializer(p, context={"request": request}).data
            async for p in qs
        ]
        return Response({"count": len(results), "results": results})


# =====================================================================
#  ADMIN ONBOARDING REVIEW — list all customers with their profile + score
# =====================================================================
class CustomerOnboardingListView(ListAPIView):
    """Admin list of customers — profile state, KYC state, score.

    Query params:
      kyc_status   — pending / approved / rejected
      profile_complete — true / false
      q            — search by name/email/cnic
    """
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    serializer_class = CustomerProfileSerializer

    def get_queryset(self):
        qs = CustomerProfile.objects.select_related(
            "user", "kyc_reviewed_by",
        ).defer(
            "kyc_last_resubmit_at", "kyc_last_resubmit_changes"
        ).order_by("-created_at")
        p = self.request.query_params
        # Accept either `kyc_status` or the shorter `kyc` from older UIs.
        kyc_val = p.get("kyc_status") or p.get("kyc")
        if kyc_val and kyc_val != "all":
            # "pending" is a meta-bucket that also covers resubmitted profiles
            # — both need admin attention. The dedicated "resubmitted" tab is
            # still available to filter them out if needed.
            if kyc_val == "pending":
                qs = qs.filter(kyc_status__in=[
                    CustomerProfile.KYC_PENDING,
                    CustomerProfile.KYC_RESUBMITTED,
                ])
            else:
                qs = qs.filter(kyc_status=kyc_val)
        q = p.get("q")
        if q:
            from django.db.models import Q
            qs = qs.filter(
                Q(full_name__icontains=q) |
                Q(user__email__icontains=q) |
                Q(cnic_number__icontains=q)
            )
        return qs

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        # Attach score to each row
        results = response.data.get("results", response.data) \
            if isinstance(response.data, dict) else response.data

        # Batch-compute scores
        from myapp.Models.Auth_models import User
        user_ids = [r["user"] for r in results]
        users = {str(u.pk): u for u in User.objects.filter(pk__in=user_ids)}
        for r in results:
            u = users.get(r["user"])
            r["score"] = compute_score(u) if u else None

        if isinstance(response.data, dict):
            response.data["results"] = results
        else:
            response.data = results
        return response


# =====================================================================
#  CUSTOMER ACCOUNT DETAILS — one-shot lookup for the staff popup
# =====================================================================
class CustomerAccountDetailView(RetrieveAPIView):
    """Everything staff need about one customer in a single request.

    Customers give us their contact info and receiving accounts during
    onboarding, but staff kept re-asking for them because the data was
    split across the user record, the KYC profile and the two account
    tables. This joins all four so the "Customer details" popup on the
    transaction, onboarding and by-customer screens can open with one call.

    Read-only: edits still go through the banking / profile endpoints so
    they stay audit-logged.
    """
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    serializer_class = CustomerAccountDetailSerializer
    lookup_url_kwarg = "user_id"

    def get_queryset(self):
        return (
            User.objects.select_related("profile")
            .prefetch_related("bank_accounts__bank", "merchant_accounts__bank")
        )


# =====================================================================
#  ONBOARDING COUNTS (for sidebar badge)
# =====================================================================
from rest_framework.decorators import api_view, permission_classes as perm_classes


@api_view(["GET"])
@perm_classes([IsAuthenticated, IsAdminOrAccountant])
def onboarding_counts(request):
    """
    Lightweight endpoint that returns counts for each KYC bucket plus
    submitted-transaction count. The sidebar polls this to show badges.
    """
    from django.db.models import Count, Q
    from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus

    agg = CustomerProfile.objects.aggregate(
        pending=Count("pk", filter=Q(kyc_status=CustomerProfile.KYC_PENDING)),
        resubmitted=Count("pk", filter=Q(kyc_status=CustomerProfile.KYC_RESUBMITTED)),
        objections=Count("pk", filter=Q(kyc_status=CustomerProfile.KYC_OBJECTIONS)),
        approved=Count("pk", filter=Q(kyc_status=CustomerProfile.KYC_APPROVED)),
        rejected=Count("pk", filter=Q(kyc_status=CustomerProfile.KYC_REJECTED)),
    )
    # KYC badge: anything awaiting an admin decision
    agg["awaiting_review"] = (agg.get("pending") or 0) + (agg.get("resubmitted") or 0)

    # Transactions that still need accountant / admin attention
    tx_agg = IncomingPayment.objects.aggregate(
        submitted=Count("pk", filter=Q(status=TransactionStatus.SUBMITTED)),
        under_review=Count("pk", filter=Q(status=TransactionStatus.UNDER_REVIEW)),
    )
    agg["submitted_transactions"] = tx_agg.get("submitted") or 0
    agg["under_review_transactions"] = tx_agg.get("under_review") or 0
    # Combined badge for the Transactions nav item — everything still in the pipeline
    agg["pending_transactions"] = (
        (tx_agg.get("submitted") or 0) + (tx_agg.get("under_review") or 0)
    )
    return Response(agg)


# =====================================================================
#  CNIC AVAILABILITY CHECK  (used by onboarding wizard)
# =====================================================================
import re

_CNIC_RE = re.compile(r"^\d{5}-?\d{7}-?\d{1}$")


@api_view(["GET"])
@perm_classes([IsAuthenticated])
def cnic_available(request):
    """
    GET /accounts/cnic-available/?cnic=12345-1234567-1

    Returns {"available": bool, "format_valid": bool} so the
    onboarding wizard can warn the user *as they type* if the CNIC
    they're entering is already attached to another account, instead
    of waiting until form-submit to find out.

    Rules:
      - We exclude the current user's own profile (so re-entering
        their own CNIC during a re-submit is not flagged "duplicate").
      - We do not 400 on bad format — instead `format_valid: false`
        with `available: null`. The caller decides how to render.
      - This is auth-required: only signed-in users (i.e. those
        actively in onboarding) can probe. Anonymous strangers can
        not enumerate the CNIC space.
    """
    cnic = (request.query_params.get("cnic") or "").strip()
    if not cnic:
        return Response(
            {"available": None, "format_valid": False,
             "detail": "cnic query param required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not _CNIC_RE.match(cnic):
        # Don't even hit the DB for malformed input — the frontend
        # should debounce until the format is valid anyway, but we
        # answer cleanly if it asks early.
        return Response(
            {"available": None, "format_valid": False},
            status=status.HTTP_200_OK,
        )

    qs = CustomerProfile.objects.filter(cnic_number=cnic)
    # Exclude the current user's own profile: re-entering your own
    # CNIC during a re-submit must not be flagged as "duplicate".
    qs = qs.exclude(user_id=request.user.id)
    return Response({
        "available": not qs.exists(),
        "format_valid": True,
    })
