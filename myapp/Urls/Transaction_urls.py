"""Transaction URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Transaction_views import (
    IncomingPaymentViewSet, OutgoingTransferViewSet,
    customers_with_tx_counts,
)

router = DefaultRouter()
router.register(r"payments", IncomingPaymentViewSet, basename="payments")
router.register(r"transfers", OutgoingTransferViewSet, basename="transfers")

urlpatterns = [
    path("", include(router.urls)),
    path(
        "customers-summary/", customers_with_tx_counts,
        name="customers-summary",
    ),
]
