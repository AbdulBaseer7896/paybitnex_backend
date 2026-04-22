"""Invoicing URLs — clients + customer companies + payment method config."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Invoicing_views import (
    ClientViewSet, CustomerCompanyViewSet,
    PaymentMethodConfigViewSet, CustomerAllowedPaymentMethodViewSet,
    MyAllowedPaymentMethodsView, InvoiceViewSet, PublicInvoiceView,
)

router = DefaultRouter()
router.register(r"clients", ClientViewSet, basename="clients")
router.register(r"companies", CustomerCompanyViewSet, basename="companies")
router.register(r"payment-methods", PaymentMethodConfigViewSet,
                basename="payment-method-config")
router.register(r"allowed-methods", CustomerAllowedPaymentMethodViewSet,
                basename="allowed-methods")
router.register(r"invoices", InvoiceViewSet, basename="invoices")

urlpatterns = [
    path("", include(router.urls)),
    path("my-allowed-payment-methods/",
         MyAllowedPaymentMethodsView.as_view(),
         name="my-allowed-payment-methods"),
    # Public endpoint — no /invoicing prefix because it's shared
    # externally; we mount it directly at the API root via urls.py.
]
