"""Bank-reconciliation audit URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.BankAudit_views import BankAuditRunView, BankAuditViewSet

router = DefaultRouter()
router.register(r"audits", BankAuditViewSet, basename="bank-audit")

urlpatterns = [
    path("run/", BankAuditRunView.as_view(), name="bank-audit-run"),
    path("", include(router.urls)),
]
