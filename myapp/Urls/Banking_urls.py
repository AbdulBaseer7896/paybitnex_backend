"""Banking URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Banking_views import (
    PakistaniBankListView, ForeignBankListView,
    CustomerBankAccountViewSet, CustomerMerchantAccountViewSet,
)

router = DefaultRouter()
router.register(r"bank-accounts", CustomerBankAccountViewSet, basename="bank-accounts")
router.register(r"merchant-accounts", CustomerMerchantAccountViewSet, basename="merchant-accounts")

urlpatterns = [
    path("", include(router.urls)),
    path("banks/pk/", PakistaniBankListView.as_view(), name="banks-pk"),
    path("banks/foreign/", ForeignBankListView.as_view(), name="banks-foreign"),
]
