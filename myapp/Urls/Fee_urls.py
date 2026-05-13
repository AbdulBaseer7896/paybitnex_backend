"""Fee URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from myapp.Views.Fee_views import (
    CustomerFeeConfigViewSet, my_effective_fee, customer_effective_fee,
    partner_shares_info,
)

router = DefaultRouter()
router.register(r"customer-configs", CustomerFeeConfigViewSet, basename="fee-customer-configs")

urlpatterns = [
    path("", include(router.urls)),
    path("my-rate/", my_effective_fee, name="fee-my-rate"),
    path("user/<uuid:user_id>/rate/", customer_effective_fee, name="fee-user-rate"),
    path("partner-shares-info/", partner_shares_info, name="fee-partner-shares-info"),
]
