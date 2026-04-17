"""Core URLs: currencies, settings, audit log, dashboard."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Core_views import (
    CurrencyViewSet, SystemSettingViewSet,
    AuditLogListView, dashboard_summary,
)

router = DefaultRouter()
router.register(r"currencies", CurrencyViewSet, basename="currencies")
router.register(r"settings", SystemSettingViewSet, basename="settings")

urlpatterns = [
    path("", include(router.urls)),
    # Audit log (full activity feed) — both names route to the same view
    path("audit-log/", AuditLogListView.as_view(), name="audit-log"),
    path("activity/",  AuditLogListView.as_view(), name="activity"),
    path("dashboard/", dashboard_summary, name="dashboard"),
]
