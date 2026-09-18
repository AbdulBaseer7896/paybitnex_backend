"""Bank-reconciliation audit URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.BankAudit_views import (
    BankAuditRunView, BankAuditViewSet,
    BankStatementRecordListView, BankSyncJobListView,
)

router = DefaultRouter()
router.register(r"audits", BankAuditViewSet, basename="bank-audit")

urlpatterns = [
    path("run/", BankAuditRunView.as_view(), name="bank-audit-run"),
    path("ubl-records/", BankStatementRecordListView.as_view(), name="ubl-statement-records"),
    path("sync-jobs/", BankSyncJobListView.as_view(), name="bank-sync-jobs"),
    path("", include(router.urls)),
]
