"""Account URLs: admin user management + customer profile + KYC review + score."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Account_views import (
    UserAdminViewSet, CustomerProfileView,
    KYCReviewView, PendingKYCListView,
    CustomerScoreView, CustomerOnboardingListView,
)

router = DefaultRouter()
router.register(r"users", UserAdminViewSet, basename="users")

urlpatterns = [
    path("", include(router.urls)),
    path("profile/", CustomerProfileView.as_view(), name="profile"),
    path("score/", CustomerScoreView.as_view(), name="my-score"),
    path("score/<int:user_id>/", CustomerScoreView.as_view(), name="user-score"),
    path("kyc/pending/", PendingKYCListView.as_view(), name="kyc-pending"),
    path("kyc/<uuid:profile_id>/review/", KYCReviewView.as_view(), name="kyc-review"),
    path("onboarding/", CustomerOnboardingListView.as_view(), name="onboarding-list"),
    # Alias kept for frontend code that uses the older path.
    path("customers/onboarded/", CustomerOnboardingListView.as_view(),
         name="onboarding-list-alias"),
]
