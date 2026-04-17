"""Rate URLs."""
from django.urls import path
from myapp.Views.Rate_views import (
    ExchangeRateListView, ExchangeRateHistoryView,
    LiveMarketQuoteView,
    manual_override, trigger_refresh,
)

urlpatterns = [
    path("", ExchangeRateListView.as_view(), name="rates-list"),
    path("live/", LiveMarketQuoteView.as_view(), name="rates-live"),
    path("history/", ExchangeRateHistoryView.as_view(), name="rates-history"),
    path("override/", manual_override, name="rates-override"),
    path("refresh/", trigger_refresh, name="rates-refresh"),
]
