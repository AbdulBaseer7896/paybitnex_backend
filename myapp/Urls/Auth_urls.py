"""Authentication URLs."""
from django.urls import path
from myapp.Views.Auth_views import (
    LoginView, RefreshView, LogoutView, MeView, ChangePasswordView,
    SignupRequestOTPView, SignupVerifyOTPView,
    ForgotPasswordRequestOTPView, ForgotPasswordResetView,
)

urlpatterns = [
    path("login/", LoginView.as_view(), name="auth-login"),
    path("refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("logout/", LogoutView.as_view(), name="auth-logout"),
    path("me/", MeView.as_view(), name="auth-me"),
    path("change-password/", ChangePasswordView.as_view(), name="auth-change-password"),

    # OTP-based signup
    path("signup/request-otp/", SignupRequestOTPView.as_view(),
         name="auth-signup-request-otp"),
    path("signup/verify-otp/", SignupVerifyOTPView.as_view(),
         name="auth-signup-verify-otp"),

    # OTP-based password reset
    path("forgot-password/request-otp/", ForgotPasswordRequestOTPView.as_view(),
         name="auth-forgot-request-otp"),
    path("forgot-password/reset/", ForgotPasswordResetView.as_view(),
         name="auth-forgot-reset"),
]
