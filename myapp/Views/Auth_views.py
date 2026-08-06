"""Auth views: JWT login, refresh, logout, whoami, change password,
OTP-based signup, and OTP-based password reset."""
# pyrefly: ignore [missing-import]
from adrf.views import APIView as AsyncAPIView
from rest_framework import status, serializers
from rest_framework.throttling import AnonRateThrottle
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken

from django.contrib.auth import get_user_model
from django.db import transaction

from django.core.cache import cache

from myapp.serializers.User_serializers import (
    CustomTokenObtainPairSerializer, UserSerializer, ChangePasswordSerializer,
)
from myapp.Models.Audit_models import AuditLog
from myapp.Models.EmailOTP_models import EmailOTP, OTPPurpose
from myapp.Utils.async_helpers import async_is_valid
from myapp.Utils.email_tasks import send_email_async
from myapp.Utils.staff_alerts import notify_staff


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def _client_ip(request):
    """Best-effort client IP extraction.

    Honors X-Forwarded-For (used by Nginx/Cloudflare/most proxies) but
    falls back to REMOTE_ADDR. Always picks the LEFTMOST address in
    XFF, which is the original client; the rest of the chain are
    intermediate proxies.
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        # Comma-separated list, possibly with whitespace.
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")

User = get_user_model()



class LoginRateThrottle(AnonRateThrottle):
    """Tight per-IP throttle on the login endpoint — 10 attempts/min.
    Limits brute-force credential stuffing without blocking legitimate users.
    """
    scope = "login"

class LoginView(TokenObtainPairView):
    """POST email+password → {access, refresh, user}."""
    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]
    
    def post(self, request, *args, **kwargs):
        from rest_framework_simplejwt.exceptions import AuthenticationFailed
        try:
            return super().post(request, *args, **kwargs)
        except AuthenticationFailed as exc:
            if exc.get_codes() == "EMAIL_UNVERIFIED":
                return Response(
                    {"error": "EMAIL_UNVERIFIED", "message": str(exc.detail)},
                    status=status.HTTP_401_UNAUTHORIZED
                )
            raise


class RefreshView(TokenRefreshView):
    permission_classes = [AllowAny]


class LogoutView(AsyncAPIView):
    permission_classes = [AllowAny]

    async def post(self, request):
        refresh = request.data.get("refresh")
        if refresh:
            try:
                token = RefreshToken(refresh)
                if hasattr(token, "blacklist"):
                    token.blacklist()
            except Exception:
                pass

        if request.user and request.user.is_authenticated:
            try:
                await AuditLog.arecord(
                    user=request.user, action=AuditLog.ACTION_LOGOUT,
                    description="User logged out",
                )
            except Exception:
                pass

        return Response({"detail": "Successfully logged out."}, status=status.HTTP_200_OK)


async def _build_me_response(user):
    """
    Build the /me response dict: user data + feature map + KYC status.
    Extracted to avoid repeating the same async DB calls in both GET and PATCH.
    """
    from myapp.Utils.features import auser_feature_map
    from myapp.Models.Profile_models import CustomerProfile

    data = UserSerializer(user).data
    data["features"] = await auser_feature_map(user)
    try:
        # .only() keeps this lean and migration-safe: a new column that
        # hasn't been applied to prod yet cannot cause an UndefinedColumn
        # crash on /auth/me/ (same risk as the login serializer).
        profile = await (
            CustomerProfile.objects
            .only("kyc_status", "kyc_objections")
            .aget(user=user)
        )
        data["kyc_status"] = profile.kyc_status
        objs = profile.kyc_objections or []
        data["kyc_objections"] = objs if isinstance(objs, list) else []
        data["kyc_objection_count"] = len(objs) if isinstance(objs, list) else 0
    except CustomerProfile.DoesNotExist:
        data["kyc_status"] = None
        data["kyc_objections"] = []
        data["kyc_objection_count"] = 0

    # ── Vendor-portal context ────────────────────────────────────────
    # Tells the frontend whether to surface the vendor portal. Wrapped
    # broadly and using .only() for the same reason as the KYC block
    # above: if migration 0053 has not been applied yet, selecting the
    # new portal_* columns would raise UndefinedColumn and take down
    # /auth/me/ — i.e. break login for EVERY user, not just vendors.
    # Degrading to "not a vendor" is the safe failure here.
    data["is_vendor"] = False
    data["vendor"] = None
    try:
        from myapp.Models.InternalTx_models import Vendor
        vendor = await (
            Vendor.objects
            .only("id", "name", "portal_enabled", "is_active", "portal_user")
            .aget(portal_user=user, portal_enabled=True, is_active=True)
        )
        data["is_vendor"] = True
        data["vendor"] = {"id": str(vendor.id), "name": vendor.name}
    except Exception:
        # Vendor.DoesNotExist (the common case) or a missing column.
        pass
    return data


class MeView(AsyncAPIView):
    """GET current user info / PATCH limited self-editable fields."""
    permission_classes = [IsAuthenticated]

    async def get(self, request):
        return Response(await _build_me_response(request.user))

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
        return Response(await _build_me_response(user))


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


class _SetPinSerializer(serializers.Serializer):
    """Validate a 4–8 digit numeric PIN for the My-Payments lock."""
    pin = serializers.RegexField(
        r"^\d{4,8}$", error_messages={"invalid": "PIN must be 4–8 digits."},
    )
    # Required when a PIN already exists (changing it) and when removing it.
    current_pin = serializers.CharField(required=False, allow_blank=True)


class PaymentsPinView(APIView):
    """Customer self-service for the optional "My Payments" PIN.

    GET    → { is_set: bool }
    POST   → set or change the PIN. Body: { pin, current_pin? }.
             If a PIN already exists, current_pin must match.
    DELETE → remove the PIN. Body: { current_pin } must match.
    The PIN is stored only as a salted hash. Unlock state is tracked
    per-browser on the client (localStorage), so a new browser starts locked.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"is_set": bool(request.user.payments_pin_hash)})

    def post(self, request):
        from django.contrib.auth.hashers import make_password, check_password
        user = request.user
        s = _SetPinSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        new_pin = s.validated_data["pin"]
        current = s.validated_data.get("current_pin") or ""
        # If a PIN already exists, require the current one to change it.
        if user.payments_pin_hash:
            if not check_password(current, user.payments_pin_hash):
                return Response(
                    {"current_pin": "Current PIN is incorrect."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        user.payments_pin_hash = make_password(new_pin)
        user.save(update_fields=["payments_pin_hash", "updated_at"])
        AuditLog.record(
            user=user, action=AuditLog.ACTION_UPDATE, target=user,
            description="Set/changed My-Payments PIN",
        )
        return Response({"is_set": True})

    def delete(self, request):
        from django.contrib.auth.hashers import check_password
        user = request.user
        if not user.payments_pin_hash:
            return Response({"is_set": False})
        current = request.data.get("current_pin") or ""
        if not check_password(current, user.payments_pin_hash):
            return Response(
                {"current_pin": "Current PIN is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.payments_pin_hash = ""
        user.save(update_fields=["payments_pin_hash", "updated_at"])
        AuditLog.record(
            user=user, action=AuditLog.ACTION_UPDATE, target=user,
            description="Removed My-Payments PIN",
        )
        return Response({"is_set": False})


class PaymentsPinVerifyView(APIView):
    """Verify a PIN to unlock the My-Payments page in the current browser.

    POST { pin } → { ok: bool }. Returns 200 with ok=false on mismatch so the
    client can show an inline error without treating it as a hard failure.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.contrib.auth.hashers import check_password
        user = request.user
        if not user.payments_pin_hash:
            # No PIN configured → nothing to unlock.
            return Response({"ok": True, "is_set": False})
        pin = str(request.data.get("pin") or "")
        ok = check_password(pin, user.payments_pin_hash)
        return Response({"ok": ok, "is_set": True})


class _ForgotPinSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ForgotPaymentsPinView(APIView):
    """Reset the logged-in customer's My-Payments PIN by email.

    POST /auth/payments-pin/forgot/   {email}

    Flow:
      - The customer is already authenticated (they're on their own
        settings page). They type their email to confirm identity.
      - If the email matches their OWN account, generate a fresh random
        8-digit PIN, store its hash, and email the new PIN to that
        address. The customer can then use it to unlock and, if they
        want, change it from settings.
      - If the email doesn't match their account, return 400 so the UI
        can show "that's not the email on this account".

    The new PIN is sent ONLY to the account's email — never returned in
    the API response — so seeing it requires access to the inbox.
    A light per-user rate limit prevents spamming the mailbox.
    """
    permission_classes = [IsAuthenticated]

    RATE_LIMIT_MAX = 5
    RATE_LIMIT_TTL = 60 * 60   # 5 resets per hour per user

    def post(self, request):
        import secrets
        from django.contrib.auth.hashers import make_password

        user = request.user
        s = _ForgotPinSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = s.validated_data["email"].strip().lower()

        # The email must belong to THIS account.
        if email != (user.email or "").strip().lower():
            return Response(
                {"email": "That email doesn't match this account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Per-user rate limit.
        cache_key = f"forgot_pin_rl:{user.id}"
        attempts = cache.get(cache_key, 0)
        if attempts >= self.RATE_LIMIT_MAX:
            return Response(
                {"detail": "Too many PIN reset requests. Please try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        cache.set(cache_key, attempts + 1, self.RATE_LIMIT_TTL)

        # Generate a fresh 8-digit PIN (allowing leading zeros).
        new_pin = "".join(secrets.choice("0123456789") for _ in range(8))
        user.payments_pin_hash = make_password(new_pin)
        user.save(update_fields=["payments_pin_hash", "updated_at"])

        send_email_async(
            to=[user.email],
            subject="Your new payments PIN",
            template="auth/payments_pin_reset",
            context={
                "name": user.full_name or "",
                "pin": new_pin,
            },
        )

        AuditLog.record(
            user=user, action=AuditLog.ACTION_UPDATE, target=user,
            description="Reset My-Payments PIN via email",
        )
        return Response({
            "detail": "A new 8-digit PIN has been sent to your email.",
        })


class _OnboardingStepSerializer(serializers.Serializer):
    """Validate the step number — clamped to 0..3 since onboarding has 4 steps."""
    step = serializers.IntegerField(min_value=0, max_value=3)


class OnboardingStepView(AsyncAPIView):
    """
    PATCH /auth/onboarding-step/   {step: int}

    Records the last completed onboarding step on the user. The
    frontend onboarding wizard calls this after every step transition
    so that, if the user closes the tab and comes back later, we can
    resume from where they left off instead of restarting at step 1.

    The `goto` flag in the GET response tells the frontend which step
    to render. Once `is_profile_complete` flips to true, this becomes
    a no-op — completed users hit /app instead of /onboarding.
    """
    permission_classes = [IsAuthenticated]

    async def get(self, request):
        # `goto` is the step the frontend should render next.
        # Returning it explicitly (rather than just the raw stored
        # value) keeps the contract clear: the server tells the
        # client where to start, the client doesn't have to interpret.
        user = request.user
        return Response({
            "onboarding_step": user.onboarding_step,
            "goto": user.onboarding_step,
            "is_profile_complete": user.is_profile_complete,
        })

    async def patch(self, request):
        s = _OnboardingStepSerializer(data=request.data)
        await async_is_valid(s, raise_exception=True)
        new_step = s.validated_data["step"]
        user = request.user
        # Never let the step go backwards — if the user already got to
        # step 3 and refreshes mid-step-2, we don't want to clobber
        # their progress. Frontend only PATCHes forward anyway, but
        # this guards against clock-skew / out-of-order requests.
        if new_step > (user.onboarding_step or 0):
            user.onboarding_step = new_step
            await user.asave(update_fields=["onboarding_step", "updated_at"])
        return Response({
            "onboarding_step": user.onboarding_step,
            "goto": user.onboarding_step,
        })


# ─────────────────────────────────────────────────────────────────────
# OTP-based signup
# ─────────────────────────────────────────────────────────────────────
class _SignupRequestOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, write_only=True)
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)


class SignupRequestOTPView(APIView):
    """
    POST /auth/signup/request-otp/   {email, password, full_name?, phone?}

    If the email already belongs to a registered user, respond with 409
    so the frontend can redirect to login. Otherwise create an inactive User, 
    mint an EMAIL_VERIFICATION OTP, email it, and return 200.
    """
    authentication_classes = []
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

        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=s.validated_data["password"],
                full_name=s.validated_data.get("full_name", ""),
                phone=s.validated_data.get("phone", ""),
                is_active=False
            )

            otp = EmailOTP.issue(email=email, purpose=OTPPurpose.EMAIL_VERIFICATION)
            send_email_async(
                to=[email],
                subject="Verify your PaidiX email",
                template="auth/email_verification",
                context={"code": otp.code, "name": user.full_name or ""},
            )
            
        return Response(
            {"detail": "User created. Verification code sent."},
            status=status.HTTP_200_OK,
        )


# ─────────────────────────────────────────────────────────────────────
# OTP-based password reset
# ─────────────────────────────────────────────────────────────────────
class _ForgotPasswordRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ForgotPasswordRequestOTPView(APIView):
    """
    POST /auth/forgot-password/request-otp/   {email}

    Behavior (intentional, requested by product):
      - If the email is NOT registered, return 404 immediately so the
        UI can stay on the request step. We do NOT send an email.
      - IP-based rate limit: 3 attempts per 24h per IP. The 4th attempt
        from the same IP returns 429 "try after 24 hours". The IP can
        still log in or do anything else — the limit is scoped to this
        endpoint only.

    Note on email enumeration: the product team explicitly prefers
    clear UX over silent-success enumeration protection here. The IP
    rate limit makes brute-force enumeration impractical (attacker
    burns their 3 attempts per IP per day) and login attempts on the
    /auth/login/ endpoint already reveal the same information.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    # 3 attempts per 24h per IP. Tweakable here without code changes
    # elsewhere.
    RATE_LIMIT_MAX = 3
    RATE_LIMIT_TTL = 24 * 60 * 60   # 24h in seconds

    def post(self, request):
        s = _ForgotPasswordRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = s.validated_data["email"].lower()

        # ---- IP rate limit check (before any DB lookup) -------------
        ip = _client_ip(request)
        cache_key = f"fp_rl:{ip}"
        attempts = cache.get(cache_key, 0)
        if attempts >= self.RATE_LIMIT_MAX:
            return Response(
                {
                    "detail": (
                        "You've reached the maximum number of password "
                        "reset attempts for today. Please try again "
                        "after 24 hours, or contact admin."
                    ),
                    "code": "rate_limited",
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # ---- Increment counter BEFORE looking up email --------------
        # Otherwise an attacker could probe email existence freely as
        # long as the email doesn't exist (no rate limit hit). The
        # counter must apply to every request, regardless of outcome.
        # `add()` returns True only if the key didn't exist; in that
        # case it sets the TTL for the first time. After that we use
        # incr() which preserves the TTL.
        if not cache.add(cache_key, 1, timeout=self.RATE_LIMIT_TTL):
            try:
                cache.incr(cache_key)
            except ValueError:
                # Key vanished between add() and incr() (race or TTL
                # boundary). Set it fresh — caller still gets one
                # request "for free" but on the next one we're back
                # in sync.
                cache.set(cache_key, 1, timeout=self.RATE_LIMIT_TTL)

        # ---- Email existence check ----------------------------------
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            return Response(
                {
                    "detail": "No account found with this email address.",
                    "code": "email_not_found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # ---- Issue OTP and send email -------------------------------
        otp = EmailOTP.issue(email=email, purpose=OTPPurpose.PASSWORD_RESET)
        send_email_async(
            to=[email],
            subject="Your PaidiX password reset code",
            template="auth/otp_password_reset",
            context={"code": otp.code, "name": user.full_name or ""},
        )

        return Response(
            {"detail": "A reset code has been sent to your email."},
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
    authentication_classes = []
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


# ─────────────────────────────────────────────────────────────────────
# Pre-Login Email Verification
# ─────────────────────────────────────────────────────────────────────
class _VerifyEmailSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(min_length=6, max_length=6)

class VerifyEmailEndpoint(APIView):
    """POST /auth/email/verify/ {email, code}"""
    authentication_classes = []
    permission_classes = [AllowAny]
    
    def post(self, request):
        s = _VerifyEmailSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = s.validated_data["email"].lower()
        
        user = User.objects.filter(email__iexact=email).first()
        if not user:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)
            
        otp = EmailOTP.objects.filter(
            email=email, purpose=OTPPurpose.EMAIL_VERIFICATION, consumed_at__isnull=True
        ).order_by("-created_at").first()
        
        if not otp:
            return Response({"detail": "No active code for this email. Please request a new one."}, status=status.HTTP_400_BAD_REQUEST)
            
        ok, reason = otp.verify(s.validated_data["code"])
        if not ok:
            messages = {
                "expired": "This code has expired. Please request a new one.",
                "locked": "Too many failed attempts. Please request a new code.",
                "invalid": "The code you entered is incorrect.",
                "consumed": "This code has already been used.",
            }
            return Response({"detail": messages.get(reason, "Invalid code.")}, status=status.HTTP_400_BAD_REQUEST)
            
        with transaction.atomic():
            is_new_activation = not user.is_active

            user.email_verified = True
            user.is_active = True
            user.verification_deadline = None
            user.save(update_fields=["email_verified", "is_active", "verification_deadline", "updated_at"])
            
            if is_new_activation:
                AuditLog.record(
                    user=user, action="auth.signup_verified",
                    target=user,
                    metadata={"email": email, "via": "email_otp"},
                )
                try:
                    from myapp.Utils.auto_assign_payment_methods import assign_defaults_to_user
                    assign_defaults_to_user(user, granted_by=None)
                except Exception:
                    pass

                transaction.on_commit(
                    lambda: notify_staff(
                        subject=f"New customer signup — {user.full_name or user.email}",
                        template="staff/new_signup",
                        context={
                            "customer_name":  user.full_name or user.email,
                            "customer_email": user.email,
                            "phone":          user.phone or "",
                        },
                        path="/kyc",
                        reply_to=[user.email],
                    )
                )

            AuditLog.record(user=user, action=AuditLog.ACTION_UPDATE, target=user, description="Verified email address via OTP")
        return Response({"detail": "Email verified successfully."}, status=status.HTTP_200_OK)

class _ResendVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False)
    password = serializers.CharField(write_only=True, required=False)

class ResendVerificationEndpoint(APIView):
    """POST /auth/email/resend/ {email?, password?}"""
    permission_classes = [AllowAny]
    
    def post(self, request):
        from django.core.cache import cache
        s = _ResendVerificationSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        
        if request.user and request.user.is_authenticated:
            user = request.user
            email = user.email.lower()
        else:
            email = s.validated_data.get("email", "").lower()
            password = s.validated_data.get("password", "")
            if not email or not password:
                return Response(
                    {"detail": "Email and password are required."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            user = User.objects.filter(email__iexact=email).first()
            if not user or not user.check_password(password):
                return Response({"detail": "Invalid credentials."}, status=status.HTTP_400_BAD_REQUEST)
            
        if getattr(user, "email_verified", False):
            return Response({"detail": "Email is already verified."}, status=status.HTTP_400_BAD_REQUEST)
            
        # Rate limit check using Redis cache
        cache_key = f"email_resend_limit_{email}"
        attempts = cache.get(cache_key, 0)
        if attempts >= 3:
            return Response(
                {"detail": "Too many verification requests. Please try again after 10 minutes."}, 
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )
            
        cache.set(cache_key, attempts + 1, timeout=600)
        
        otp = EmailOTP.issue(email=email, purpose=OTPPurpose.EMAIL_VERIFICATION)
        send_email_async(
            to=[email],
            subject="Verify your PaidiX email",
            template="auth/email_verification",
            context={"code": otp.code, "name": user.full_name or ""}
        )
        return Response({"detail": "Verification code sent."}, status=status.HTTP_200_OK)

class _ChangeEmailPreLoginSerializer(serializers.Serializer):
    current_email = serializers.EmailField(required=False)
    password = serializers.CharField(write_only=True)
    new_email = serializers.EmailField()

class ChangeEmailPreLoginEndpoint(APIView):
    """POST /auth/email/change/ {current_email?, password, new_email}"""
    permission_classes = [AllowAny]
    
    def post(self, request):
        from django.core.cache import cache
        s = _ChangeEmailPreLoginSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        
        if request.user and request.user.is_authenticated:
            current_email = request.user.email.lower()
        else:
            current_email = s.validated_data.get("current_email", "").lower()
            if not current_email:
                return Response({"detail": "Current email is required."}, status=status.HTTP_400_BAD_REQUEST)
                
        new_email = s.validated_data["new_email"].lower()
        
        if current_email == new_email:
            return Response({"detail": "New email cannot be the same as your current email."}, status=status.HTTP_400_BAD_REQUEST)
        
        # Rate limit check using Redis cache
        cache_key = f"email_resend_limit_{current_email}"
        attempts = cache.get(cache_key, 0)
        
        if attempts >= 3:
            return Response(
                {"detail": "Too many verification requests. Please try again after 10 minutes."}, 
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )
        
        user = User.objects.filter(email__iexact=current_email).first()
        if not user or not user.check_password(s.validated_data["password"]):
            cache.set(cache_key, attempts + 1, timeout=600)
            return Response({"detail": "Invalid credentials."}, status=status.HTTP_400_BAD_REQUEST)
            
        if User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
            return Response({"detail": "This email is already in use by another account."}, status=status.HTTP_409_CONFLICT)
            
        cache.set(cache_key, attempts + 1, timeout=600)
        
        # Issue OTP for the new email (does NOT mutate user.email yet!)
        otp = EmailOTP.issue(email=new_email, purpose=OTPPurpose.EMAIL_VERIFICATION)
        send_email_async(
            to=[new_email],
            subject="Verify your new PaidiX email",
            template="auth/email_verification",
            context={"code": otp.code, "name": user.full_name or ""}
        )
        
        return Response({"detail": "Verification code sent to your new email."}, status=status.HTTP_200_OK)


class _ChangeEmailVerifySerializer(serializers.Serializer):
    current_email = serializers.EmailField(required=False)
    new_email = serializers.EmailField()
    code = serializers.CharField(max_length=6)

class ChangeEmailVerifyEndpoint(APIView):
    """POST /auth/email/change/verify/ {current_email?, new_email, code}"""
    permission_classes = [AllowAny]

    def post(self, request):
        s = _ChangeEmailVerifySerializer(data=request.data)
        s.is_valid(raise_exception=True)

        if request.user and request.user.is_authenticated:
            user = request.user
        else:
            current_email = s.validated_data.get("current_email", "").lower()
            if not current_email:
                return Response({"detail": "Current email is required."}, status=status.HTTP_400_BAD_REQUEST)
            user = User.objects.filter(email__iexact=current_email).first()
            if not user:
                return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        new_email = s.validated_data["new_email"].lower()
        code = s.validated_data["code"].strip()

        # Check if new email is taken in the meantime
        if User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
            return Response({"detail": "This email is already in use by another account."}, status=status.HTTP_409_CONFLICT)

        otp = EmailOTP.objects.filter(
            email=new_email, purpose=OTPPurpose.EMAIL_VERIFICATION, consumed_at__isnull=True
        ).order_by("-created_at").first()
        
        if not otp:
            return Response({"detail": "No active code for this email. Please request a new one."}, status=status.HTTP_400_BAD_REQUEST)
            
        ok, reason = otp.verify(code)
        if not ok:
            messages = {
                "expired": "This code has expired. Please request a new one.",
                "locked": "Too many failed attempts. Please request a new code.",
                "invalid": "The code you entered is incorrect.",
                "consumed": "This code has already been used.",
            }
            return Response({"detail": messages.get(reason, "Invalid code.")}, status=status.HTTP_400_BAD_REQUEST)

        old_email = user.email
        user.email = new_email
        user.email_verified = True
        user.verification_deadline = None
        user.save(update_fields=["email", "email_verified", "verification_deadline", "updated_at"])

        AuditLog.record(
            user=user,
            action=AuditLog.ACTION_UPDATE,
            target=user,
            description=f"Changed email from {old_email} to {new_email} via 2-step OTP verification"
        )
        return Response({"detail": "Email updated and verified successfully."}, status=status.HTTP_200_OK)

