"""Authentication URLs."""
from django.urls import path
from myapp.Views.Auth_views import (
    LoginView, RefreshView, LogoutView, MeView, ChangePasswordView,
    SignupRequestOTPView,
    ForgotPasswordRequestOTPView, ForgotPasswordResetView,
    OnboardingStepView,
    PaymentsPinView, PaymentsPinVerifyView, ForgotPaymentsPinView,
    VerifyEmailEndpoint, ResendVerificationEndpoint, ChangeEmailPreLoginEndpoint,
    ChangeEmailVerifyEndpoint,
)

urlpatterns = [
    path("login/", LoginView.as_view(), name="auth-login"),
    path("refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("logout/", LogoutView.as_view(), name="auth-logout"),
    path("me/", MeView.as_view(), name="auth-me"),
    path("change-password/", ChangePasswordView.as_view(), name="auth-change-password"),
    path("payments-pin/", PaymentsPinView.as_view(), name="auth-payments-pin"),
    path("payments-pin/verify/", PaymentsPinVerifyView.as_view(),
         name="auth-payments-pin-verify"),
    path("payments-pin/forgot/", ForgotPaymentsPinView.as_view(),
         name="auth-payments-pin-forgot"),
    path("onboarding-step/", OnboardingStepView.as_view(),
         name="auth-onboarding-step"),

    # OTP-based signup
    path("signup/request-otp/", SignupRequestOTPView.as_view(),
         name="auth-signup-request-otp"),

    # Pre-Login Email Verification
    path("email/verify/", VerifyEmailEndpoint.as_view(), name="auth-email-verify"),
    path("email/resend/", ResendVerificationEndpoint.as_view(), name="auth-email-resend"),
    path("email-change/request/", ChangeEmailPreLoginEndpoint.as_view(), name="auth-email-change-request"),
    path("email-change/verify/", ChangeEmailVerifyEndpoint.as_view(), name="auth-email-change-verify"),
    # Legacy alias
    path("email/change/", ChangeEmailPreLoginEndpoint.as_view(), name="auth-email-change"),
    path("email/change/verify/", ChangeEmailVerifyEndpoint.as_view(), name="auth-email-change-verify"),

    # OTP-based password reset
    path("forgot-password/request-otp/", ForgotPasswordRequestOTPView.as_view(),
         name="auth-forgot-request-otp"),
    path("forgot-password/reset/", ForgotPasswordResetView.as_view(),
         name="auth-forgot-reset"),
]
