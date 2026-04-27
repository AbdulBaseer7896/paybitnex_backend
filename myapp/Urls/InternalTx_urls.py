"""Internal-transactions URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.InternalTx_views import (
    VendorViewSet, USABankAccountViewSet, CreditCardViewSet,
    InternalPakistaniAccountViewSet, InternalTransactionViewSet,
)

router = DefaultRouter()
router.register(r"vendors", VendorViewSet, basename="internal-vendors")
router.register(r"usa-bank-accounts", USABankAccountViewSet,
                basename="internal-usa-bank-accounts")
router.register(r"credit-cards", CreditCardViewSet,
                basename="internal-credit-cards")
router.register(r"pk-accounts", InternalPakistaniAccountViewSet,
                basename="internal-pk-accounts")
router.register(r"transactions", InternalTransactionViewSet,
                basename="internal-transactions")

urlpatterns = [
    path("", include(router.urls)),
]
