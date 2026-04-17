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
)
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant
from myapp.Utils.async_helpers import async_is_valid, async_save
from myapp.Utils.customer_scoring import compute_score


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

    @action(detail=True, methods=["post"], url_path="reset_password")
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

    @action(detail=True, methods=["post"], url_path="toggle_active")
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
        await request.user.asave(
            update_fields=["is_profile_complete", "full_name", "phone"],
        )

        return Response(
            CustomerProfileSerializer(profile, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    async def patch(self, request):
        try:
            profile = await CustomerProfile.objects.select_related(
                "user", "kyc_reviewed_by"
            ).aget(user=request.user)
        except CustomerProfile.DoesNotExist:
            return Response({"detail": "Profile not set up."},
                            status=status.HTTP_404_NOT_FOUND)

        s = CustomerProfileSerializer(
            profile, data=request.data, partial=True,
            context={"request": request},
        )
        await async_is_valid(s, raise_exception=True)
        profile = await async_save(s)
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
            ).aget(pk=profile_id)
        except CustomerProfile.DoesNotExist:
            return Response({"detail": "Not found."},
                            status=status.HTTP_404_NOT_FOUND)

        s = KYCReviewSerializer(data=request.data)
        await async_is_valid(s, raise_exception=True)

        before = profile.kyc_status
        profile.kyc_status = s.validated_data["status"]
        profile.kyc_notes = s.validated_data.get("notes", "")
        profile.kyc_reviewed_by = request.user
        profile.kyc_reviewed_at = timezone.now()
        await profile.asave(update_fields=[
            "kyc_status", "kyc_notes", "kyc_reviewed_by", "kyc_reviewed_at",
        ])
        await AuditLog.arecord(
            user=request.user, action=AuditLog.ACTION_KYC_REVIEW, target=profile,
            description=f"KYC {before} → {profile.kyc_status} for {profile.full_name}",
            before={"kyc_status": before},
            after={"kyc_status": profile.kyc_status},
        )
        return Response(CustomerProfileSerializer(profile).data)


class PendingKYCListView(AsyncAPIView):
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]

    async def get(self, request):
        qs = CustomerProfile.objects.filter(
            kyc_status=CustomerProfile.KYC_PENDING,
        ).select_related("user", "kyc_reviewed_by").order_by("created_at")
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
        ).order_by("-created_at")
        p = self.request.query_params
        # Accept either `kyc_status` or the shorter `kyc` from older UIs.
        kyc_val = p.get("kyc_status") or p.get("kyc")
        if kyc_val and kyc_val != "all":
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
