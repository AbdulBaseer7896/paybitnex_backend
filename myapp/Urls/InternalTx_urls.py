"""Internal-transactions URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.InternalTx_views import (
    VendorViewSet, USABankAccountViewSet, CreditCardViewSet,
    InternalPakistaniAccountViewSet, InternalTransactionViewSet,
    VendorPKRPaymentViewSet,
)
from myapp.Views.Vendor_admin_views import (
    grant_portal, revoke_portal, portal_candidates,
)

router = DefaultRouter()
router.register(r"vendors", VendorViewSet, basename="internal-vendors")
router.register(r"usa-bank-accounts", USABankAccountViewSet,
                basename="internal-usa-bank-accounts")
router.register(r"credit-cards", CreditCardViewSet,
                basename="internal-credit-cards")
router.register(r"pk-accounts", InternalPakistaniAccountViewSet,
                basename="internal-pk-accounts")
router.register(r"vendor-pkr-payments", VendorPKRPaymentViewSet,
                basename="vendor-pkr-payments")
router.register(r"transactions", InternalTransactionViewSet,
                basename="internal-transactions")

urlpatterns = [
    # Vendor-portal administration. Declared BEFORE the router include so
    # "vendors/portal-candidates/" is not swallowed by the router's
    # "vendors/<pk>/" detail route.
    path("vendors/portal-candidates/", portal_candidates,
         name="vendor-portal-candidates"),
    path("vendors/<uuid:pk>/grant-portal/", grant_portal,
         name="vendor-grant-portal"),
    path("vendors/<uuid:pk>/revoke-portal/", revoke_portal,
         name="vendor-revoke-portal"),
    path("", include(router.urls)),
]
