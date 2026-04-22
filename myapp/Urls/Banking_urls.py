"""Banking URLs.

Note: the `merchant-accounts` endpoint has been deprecated. Merchant accounts
were removed from the customer-facing workflow (the New Payment form no
longer collects them). We still keep the underlying model + viewset in the
codebase so historical records remain queryable via the Django admin, but
the public API route is no longer exposed.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Banking_views import (
    PakistaniBankListView, ForeignBankListView,
    CustomerBankAccountViewSet,
)

router = DefaultRouter()
router.register(r"bank-accounts", CustomerBankAccountViewSet, basename="bank-accounts")

urlpatterns = [
    path("", include(router.urls)),
    path("banks/pk/", PakistaniBankListView.as_view(), name="banks-pk"),
    path("banks/foreign/", ForeignBankListView.as_view(), name="banks-foreign"),
]
