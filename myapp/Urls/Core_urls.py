"""Core URLs: currencies, payment methods, settings, audit log, dashboard."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Core_views import (
    CurrencyViewSet, PaymentMethodViewSet, SystemSettingViewSet,
    AuditLogListView, dashboard_summary, bank_balances,
)
from myapp.Views.Feature_views import FeatureRegistryView

router = DefaultRouter()
router.register(r"currencies", CurrencyViewSet, basename="currencies")
router.register(r"payment-methods", PaymentMethodViewSet, basename="payment-methods")
router.register(r"settings", SystemSettingViewSet, basename="settings")

urlpatterns = [
    path("", include(router.urls)),
    path("audit-log/", AuditLogListView.as_view(), name="audit-log"),
    path("activity/",  AuditLogListView.as_view(), name="activity"),
    path("dashboard/", dashboard_summary, name="dashboard"),
    path("bank-balances/", bank_balances, name="bank-balances"),
    path("features/", FeatureRegistryView.as_view(), name="feature-registry"),
]
