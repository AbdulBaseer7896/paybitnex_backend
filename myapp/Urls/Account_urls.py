"""Account URLs: admin user management + customer profile + KYC review + score."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Account_views import (
    UserAdminViewSet, CustomerProfileView,
    KYCReviewView, PendingKYCListView, KYCRaiseObjectionsView,
    CustomerScoreView, CustomerOnboardingListView,
    onboarding_counts, cnic_available,
)
from myapp.Views.Feature_views import UserFeaturesView

router = DefaultRouter()
router.register(r"users", UserAdminViewSet, basename="users")

urlpatterns = [
    path("", include(router.urls)),
    path("profile/", CustomerProfileView.as_view(), name="profile"),
    path("score/", CustomerScoreView.as_view(), name="my-score"),
    # NOTE: <int:...> could NEVER match — User.pk is a UUID, so this route
    # was dead and every staff score lookup 404'd. Corrected to <uuid:...>.
    path("score/<uuid:user_id>/", CustomerScoreView.as_view(), name="user-score"),
    # Staff-facing lookups used by the admin Users detail drawer. These
    # paths were being called by the frontend but had no route at all,
    # producing the 404s in the browser console.
    path("users/<uuid:user_id>/profile/", CustomerProfileView.as_view(),
         name="user-profile"),
    path("users/<uuid:user_id>/score/", CustomerScoreView.as_view(),
         name="user-score-detail"),
    path("kyc/pending/", PendingKYCListView.as_view(), name="kyc-pending"),
    path("kyc/<uuid:profile_id>/review/", KYCReviewView.as_view(), name="kyc-review"),
    path("kyc/<uuid:profile_id>/objections/",
         KYCRaiseObjectionsView.as_view(), name="kyc-objections"),
    path("onboarding/", CustomerOnboardingListView.as_view(), name="onboarding-list"),
    path("onboarding/counts/", onboarding_counts, name="onboarding-counts"),
    path("cnic-available/", cnic_available, name="cnic-available"),
    # Alias kept for frontend code that uses the older path.
    path("customers/onboarded/", CustomerOnboardingListView.as_view(),
         name="onboarding-list-alias"),
    # Admin-only: read / write a customer's premium feature grants.
    path("users/<uuid:user_id>/features/",
         UserFeaturesView.as_view(), name="user-features"),
]
