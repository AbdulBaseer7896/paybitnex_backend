"""Bank-reconciliation audit URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.BankAudit_views import (
    BankAuditRunView, BankAuditViewSet,
    BankStatementRecordListView, BankSyncJobListView,
    UblVerifyView, UblVerifyStatusView,
)

router = DefaultRouter()
router.register(r"audits", BankAuditViewSet, basename="bank-audit")

urlpatterns = [
    path("run/", BankAuditRunView.as_view(), name="bank-audit-run"),
    path("ubl-records/", BankStatementRecordListView.as_view(), name="ubl-statement-records"),
    path("sync-jobs/", BankSyncJobListView.as_view(), name="bank-sync-jobs"),
    path("ubl-verify/", UblVerifyView.as_view(), name="ubl-auto-verify"),
    path("ubl-verify/latest/", UblVerifyStatusView.as_view(), {"pk": "latest"}, name="ubl-verify-latest"),
    path("ubl-verify/<int:pk>/", UblVerifyStatusView.as_view(), name="ubl-verify-status"),
    path("", include(router.urls)),
]
