"""Transaction URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Transaction_views import (
    IncomingPaymentViewSet, OutgoingTransferViewSet,
    customers_with_tx_counts,
)
from myapp.Views.Invoice_views import single_invoice_pdf, bulk_invoice_pdf

router = DefaultRouter()
router.register(r"payments", IncomingPaymentViewSet, basename="payments")
router.register(r"transfers", OutgoingTransferViewSet, basename="transfers")

urlpatterns = [
    # Invoice endpoints — registered BEFORE the router so the explicit
    # paths take precedence over any viewset detail match.
    path(
        "payments/invoice-bulk.pdf", bulk_invoice_pdf,
        name="payments-invoice-bulk",
    ),
    path(
        "payments/<uuid:payment_id>/invoice.pdf", single_invoice_pdf,
        name="payments-invoice-single",
    ),
    path("", include(router.urls)),
    path(
        "customers-summary/", customers_with_tx_counts,
        name="customers-summary",
    ),
]
