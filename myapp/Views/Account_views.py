"""
Account views:
  - Admin CRUD on users (customers, accountants, admins) with reset
    password and toggle-active actions.
  - Customer profile CRUD.
  - Customer score endpoint.
  - Accountant/admin KYC review.
  - Admin "onboarding review" — list recent customers to verify.
"""
from adrf.views import APIView as AsyncAPIView
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.Models.Auth_models import User, UserRole
from myapp.Models.Profile_models import CustomerProfile
from myapp.Models.Audit_models import AuditLog
from myapp.serializers.User_serializers import (
    UserSerializer, AdminCreateUserSerializer,
    AdminUpdateUserSerializer, AdminResetPasswordSerializer,
)
from myapp.serializers.Profile_serializers import (
    CustomerProfileSerializer, KYCReviewSerializer,
    KYCRaiseObjectionsSerializer,
)
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant
from myapp.Utils.async_helpers import async_is_valid, async_save
from myapp.Utils.customer_scoring import compute_score
from myapp.Utils.email_tasks import send_email_async


# =====================================================================
#  ADMIN USER CRUD
# =====================================================================
class UserAdminViewSet(viewsets.ModelViewSet):
    """Admin-only: list / create / update / delete users.

    Extra actions:
      POST /users/{id}/reset_password/   → force-reset password
      POST /users/{id}/toggle_active/    → flip is_active
    """
    permission_classes = [IsAuthenticated, IsAdmin]
    queryset = User.objects.all().order_by("-created_at")
    serializer_class = UserSerializer
    filterset_fields = ["role", "is_active", "is_profile_complete"]
    search_fields = ["email", "full_name", "phone"]

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

    def create(self, request, *args, **kwargs):
        s = self.get_serializer(data=request.data)
        s.is_valid(raise_exception=True)
        self.perform_create(s)
        user = s.instance
        return Response(
            {
                **UserSerializer(user).data,
                "temporary_password": getattr(user, "_plain_password", None),
            },
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        before = {
            "full_name": instance.full_name, "role": instance.role,
            "is_active": instance.is_active, "phone": instance.phone,
        }
        s = self.get_serializer(instance, data=request.data, partial=partial)
        s.is_valid(raise_exception=True)
        self.perform_update(s)
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_UPDATE, target=instance,
            description=f"Admin updated user {instance.email}",
            before=before,
            after={
                "full_name": instance.full_name, "role": instance.role,
                "is_active": instance.is_active, "phone": instance.phone,
            },
        )
        return Response(UserSerializer(instance).data)

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
        return Response(UserSerializer(user).data)


# =====================================================================
#  CUSTOMER PROFILE
# =====================================================================
class CustomerProfileView(AsyncAPIView):
    permission_classes = [IsAuthenticated]

    async def get(self, request):
        try:
            profile = await CustomerProfile.objects.select_related(
                "user", "kyc_reviewed_by"
            ).defer(
                "kyc_last_resubmit_at", "kyc_last_resubmit_changes"
            ).aget(
                user=request.user
            )
        except CustomerProfile.DoesNotExist:
            return Response({"detail": "Profile not set up."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(
            CustomerProfileSerializer(profile, context={"request": request}).data
        )

    async def post(self, request):
        if await CustomerProfile.objects.filter(user=request.user).aexists():
            return Response({"detail": "Profile already exists. Use PATCH."},
                            status=status.HTTP_400_BAD_REQUEST)

        s = CustomerProfileSerializer(
            data=request.data, context={"request": request},
        )
        await async_is_valid(s, raise_exception=True)
        profile = await async_save(s, user=request.user)

        request.user.is_profile_complete = True
        request.user.full_name = profile.full_name
        request.user.phone = profile.phone
        # Mark all 4 onboarding steps as completed. If the user logs in
        # again after this, the resume endpoint sees step=4 (== STEPS.length)
        # which the frontend interprets as "no resume needed, send to /app".
        request.user.onboarding_step = 4
        await request.user.asave(
            update_fields=["is_profile_complete", "full_name", "phone",
                           "onboarding_step"],
        )

        return Response(
            CustomerProfileSerializer(profile, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    async def patch(self, request):
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
            except User.DoesNotExist:
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

        before = profile.kyc_status
        new_status = s.validated_data["status"]
        profile.kyc_status = new_status
        profile.kyc_notes = s.validated_data.get("notes", "")
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
                    subject="Your PayBitnex account has been verified",
                    template="kyc/approved",
                    context={"name": customer_name},
                )
            except Exception:
                # Email failure must not block the verification response.
                pass

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

        now = timezone.now()
        raised_by_email = getattr(request.user, "email", "")
        new_items = [
            {
                "field": item["field"],
                "message": item["message"],
                "raised_at": now.isoformat(),
                "raised_by": raised_by_email,
            }
            for item in s.validated_data["objections"]
        ]

        before_status = profile.kyc_status
        profile.kyc_objections = new_items
        profile.kyc_objection_round = (profile.kyc_objection_round or 0) + 1
        profile.kyc_status = CustomerProfile.KYC_OBJECTIONS
        profile.kyc_notes = s.validated_data.get("notes", profile.kyc_notes or "")
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
                    subject="Action required: PayBitnex profile objections",
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
