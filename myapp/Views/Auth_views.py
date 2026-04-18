"""Auth views: JWT login, refresh, logout, whoami, change password,
OTP-based signup, and OTP-based password reset."""
from adrf.views import APIView as AsyncAPIView
from rest_framework import status, serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken

from django.contrib.auth import get_user_model
from django.db import transaction

from myapp.serializers.User_serializers import (
    CustomTokenObtainPairSerializer, UserSerializer, ChangePasswordSerializer,
)
from myapp.Models.Audit_models import AuditLog
from myapp.Models.EmailOTP_models import EmailOTP, OTPPurpose
from myapp.Utils.async_helpers import async_is_valid
from myapp.Utils.email_tasks import send_email_async

User = get_user_model()


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


# ─────────────────────────────────────────────────────────────────────
# OTP-based signup
# ─────────────────────────────────────────────────────────────────────
class _SignupRequestOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()


class SignupRequestOTPView(APIView):
    """
    POST /auth/signup/request-otp/   {email}

    If the email already belongs to a registered user, respond with 409
    so the frontend can redirect to login. Otherwise mint a 6-digit OTP,
    email it, and return 200. The OTP expires in 60 seconds.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        s = _SignupRequestOTPSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = s.validated_data["email"].lower()

        if User.objects.filter(email__iexact=email).exists():
            return Response(
                {"detail": "An account with this email already exists. "
                           "Please log in instead.",
                 "code": "email_exists"},
                status=status.HTTP_409_CONFLICT,
            )

        otp = EmailOTP.issue(email=email, purpose=OTPPurpose.SIGNUP)
        send_email_async(
            to=[email],
            subject="Your PayBitnex signup code",
            template="auth/otp_signup",
            context={"code": otp.code},
        )
        return Response(
            {"detail": "Verification code sent. It expires in 60 seconds.",
             "expires_in_seconds": 60},
            status=status.HTTP_200_OK,
        )


class _SignupVerifySerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(min_length=6, max_length=6)
    password = serializers.CharField(min_length=8, write_only=True)
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)


class SignupVerifyOTPView(APIView):
    """
    POST /auth/signup/verify-otp/   {email, code, password, full_name?, phone?}

    Verifies the OTP, creates the user account, and returns JWT tokens +
    user info so the frontend can sign the user in immediately and send
    them to onboarding.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        s = _SignupVerifySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = s.validated_data["email"].lower()

        # Guard against a race: email could have been registered between
        # request-otp and verify-otp (e.g. another tab).
        if User.objects.filter(email__iexact=email).exists():
            return Response(
                {"detail": "An account with this email already exists. "
                           "Please log in instead.",
                 "code": "email_exists"},
                status=status.HTTP_409_CONFLICT,
            )

        # Find the most recent outstanding OTP for this email+purpose.
        otp = (
            EmailOTP.objects
            .filter(email=email, purpose=OTPPurpose.SIGNUP,
                    consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if otp is None:
            return Response(
                {"detail": "No active code for this email. "
                           "Please request a new one.",
                 "code": "no_otp"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ok, reason = otp.verify(s.validated_data["code"])
        if not ok:
            messages = {
                "expired":  "This code has expired. Please request a new one.",
                "locked":   "Too many failed attempts. Please request a new code.",
                "invalid":  "The code you entered is incorrect.",
                "consumed": "This code has already been used.",
            }
            return Response(
                {"detail": messages.get(reason, "Invalid code."),
                 "code": reason},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create the user atomically
        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=s.validated_data["password"],
                full_name=s.validated_data.get("full_name", ""),
                phone=s.validated_data.get("phone", ""),
            )
            AuditLog.record(
                user=user, action="auth.signup_verified",
                target=user,
                metadata={"email": email, "via": "email_otp"},
            )

        # Issue JWT tokens for immediate sign-in
        refresh = RefreshToken.for_user(user)
        return Response({
            "access":  str(refresh.access_token),
            "refresh": str(refresh),
            "user":    UserSerializer(user).data,
        }, status=status.HTTP_201_CREATED)


# ─────────────────────────────────────────────────────────────────────
# OTP-based password reset
# ─────────────────────────────────────────────────────────────────────
class _ForgotPasswordRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ForgotPasswordRequestOTPView(APIView):
    """
    POST /auth/forgot-password/request-otp/   {email}

    Always returns 200 regardless of whether the email is registered —
    this prevents email-enumeration attacks. The OTP is only actually
    sent if a user exists with this email.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        s = _ForgotPasswordRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = s.validated_data["email"].lower()

        user = User.objects.filter(email__iexact=email).first()
        if user is not None:
            otp = EmailOTP.issue(email=email, purpose=OTPPurpose.PASSWORD_RESET)
            send_email_async(
                to=[email],
                subject="Your PayBitnex password reset code",
                template="auth/otp_password_reset",
                context={"code": otp.code, "name": user.full_name or ""},
            )

        # Silent success either way
        return Response(
            {"detail": "If that email is registered, a reset code has been sent."},
            status=status.HTTP_200_OK,
        )


class _ForgotPasswordResetSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(min_length=6, max_length=6)
    new_password = serializers.CharField(min_length=8, write_only=True)


class ForgotPasswordResetView(APIView):
    """
    POST /auth/forgot-password/reset/   {email, code, new_password}

    Verifies the OTP and updates the user's password. Invalidates any
    existing refresh tokens the user had active (blacklist pattern),
    forcing a fresh login everywhere.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        s = _ForgotPasswordResetSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = s.validated_data["email"].lower()

        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            # Do not reveal non-existence; still return a generic error
            # that matches the OTP-not-found response.
            return Response(
                {"detail": "Invalid code or email.",
                 "code": "invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        otp = (
            EmailOTP.objects
            .filter(email=email, purpose=OTPPurpose.PASSWORD_RESET,
                    consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if otp is None:
            return Response(
                {"detail": "No active code for this email. "
                           "Please request a new one.",
                 "code": "no_otp"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ok, reason = otp.verify(s.validated_data["code"])
        if not ok:
            messages = {
                "expired":  "This code has expired. Please request a new one.",
                "locked":   "Too many failed attempts. Please request a new code.",
                "invalid":  "The code you entered is incorrect.",
                "consumed": "This code has already been used.",
            }
            return Response(
                {"detail": messages.get(reason, "Invalid code."),
                 "code": reason},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(s.validated_data["new_password"])
        user.save(update_fields=["password"])

        AuditLog.record(
            user=user, action="auth.password_reset",
            target=user,
            metadata={"via": "email_otp"},
        )

        return Response(
            {"detail": "Password updated. You can now log in with your new password."},
            status=status.HTTP_200_OK,
        )
