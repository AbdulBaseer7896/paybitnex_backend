"""Fee URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from myapp.Views.Fee_views import CustomerFeeConfigViewSet

router = DefaultRouter()
router.register(r"customer-configs", CustomerFeeConfigViewSet, basename="fee-customer-configs")

urlpatterns = [
    path("", include(router.urls)),
]
