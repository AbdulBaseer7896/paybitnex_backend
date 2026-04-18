"""Auth views: JWT login, refresh, logout, whoami, change password."""
from adrf.views import APIView as AsyncAPIView
from rest_framework import status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken

from myapp.serializers.User_serializers import (
    CustomTokenObtainPairSerializer, UserSerializer, ChangePasswordSerializer,
)
from myapp.Models.Audit_models import AuditLog
from myapp.Utils.async_helpers import async_is_valid


class LoginView(TokenObtainPairView):
    """POST email+password → {access, refresh, user}."""
    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [AllowAny]


class RefreshView(TokenRefreshView):
    permission_classes = [AllowAny]


class LogoutView(AsyncAPIView):
    permission_classes = [IsAuthenticated]

    async def post(self, request):
        refresh = request.data.get("refresh")
        if not refresh:
            return Response({"detail": "refresh token required"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            token = RefreshToken(refresh)
            token.blacklist()
        except Exception as e:
            return Response({"detail": str(e)},
                            status=status.HTTP_400_BAD_REQUEST)
        await AuditLog.arecord(
            user=request.user, action=AuditLog.ACTION_LOGOUT,
            description="User logged out",
        )
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(AsyncAPIView):
    """GET current user info / PATCH limited self-editable fields."""
    permission_classes = [IsAuthenticated]

    async def get(self, request):
        data = UserSerializer(request.user).data
        return Response(data)

    async def patch(self, request):
        user = request.user
        # Whitelisted fields the user may edit on their own account
        before = {
            "full_name": user.full_name,
            "phone": user.phone,
            "had_picture": bool(user.profile_picture),
        }
        updated_fields = []
        if "full_name" in request.data:
            user.full_name = request.data["full_name"]
            updated_fields.append("full_name")
        if "phone" in request.data:
            user.phone = request.data["phone"]
            updated_fields.append("phone")
        # File upload
        if "profile_picture" in request.FILES:
            user.profile_picture = request.FILES["profile_picture"]
            updated_fields.append("profile_picture")
        if updated_fields:
            updated_fields.append("updated_at")
            await user.asave(update_fields=updated_fields)
            await AuditLog.arecord(
                user=user, action=AuditLog.ACTION_UPDATE, target=user,
                description=f"Self-updated profile fields: {', '.join(updated_fields)}",
                before=before,
                after={
                    "full_name": user.full_name,
                    "phone": user.phone,
                    "had_picture": bool(user.profile_picture),
                },
            )
        return Response(UserSerializer(user).data)


class ChangePasswordView(AsyncAPIView):
    permission_classes = [IsAuthenticated]

    async def post(self, request):
        s = ChangePasswordSerializer(data=request.data)
        # Validators here are pure (django's validate_password on a plain
        # CharField). Using the async helper anyway — safe for future edits.
        await async_is_valid(s, raise_exception=True)
        user = request.user
        if not user.check_password(s.validated_data["old_password"]):
            return Response({"detail": "Current password is incorrect."},
                            status=status.HTTP_400_BAD_REQUEST)
        user.set_password(s.validated_data["new_password"])
        await user.asave(update_fields=["password"])
        return Response({"detail": "Password changed."})
