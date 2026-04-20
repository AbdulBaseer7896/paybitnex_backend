"""Partner URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from myapp.Views.Partner_views import PartnerViewSet, PartnerLedgerListView

# Router for ledger listing (non-conflicting prefix)
router = DefaultRouter()
router.register(r"ledger", PartnerLedgerListView, basename="partner-ledger")

# Explicit routes for the partners viewset (cleaner than empty-prefix routing)
partner_list = PartnerViewSet.as_view({"get": "list", "post": "create"})
partner_detail = PartnerViewSet.as_view({
    "get": "retrieve",
    "put": "update",
    "patch": "partial_update",
    "delete": "destroy",
})
partner_ledger_action = PartnerViewSet.as_view({"get": "ledger"})
partner_balance_action = PartnerViewSet.as_view({"get": "balance"})
partner_bulk_shares = PartnerViewSet.as_view({"post": "bulk_update_shares"})
partner_recompute = PartnerViewSet.as_view({"post": "recompute_ledger"})
partner_report_pdf_action = PartnerViewSet.as_view({"get": "report_pdf"})

urlpatterns = [
    path("", include(router.urls)),
    path("list/", partner_list, name="partners-list"),
    path("shares/bulk/", partner_bulk_shares, name="partners-shares-bulk"),
    path("recompute-ledger/", partner_recompute, name="partners-recompute-ledger"),
    path("<uuid:pk>/", partner_detail, name="partners-detail"),
    path("<uuid:pk>/ledger/", partner_ledger_action, name="partners-ledger"),
    path("<uuid:pk>/balance/", partner_balance_action, name="partners-balance"),
    path("<uuid:pk>/report.pdf", partner_report_pdf_action, name="partners-report-pdf"),
]
