"""Transaction URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Transaction_views import (
    IncomingPaymentViewSet, OutgoingTransferViewSet,
)

router = DefaultRouter()
router.register(r"payments", IncomingPaymentViewSet, basename="payments")
router.register(r"transfers", OutgoingTransferViewSet, basename="transfers")

urlpatterns = [
    path("", include(router.urls)),
]
