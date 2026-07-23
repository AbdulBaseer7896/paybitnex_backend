"""Vendor-portal URLs.

Mounted at /api/v1/vendor/. Every view here is guarded by
IsVendorPortalUser and scoped to the caller's own vendor — see
Views/Vendor_portal_views.py for the scoping rule.
"""
from django.urls import path

from myapp.Views.Vendor_portal_views import (
    vendor_me, vendor_dashboard, vendor_transactions, vendor_transactions_csv,
    vendor_cards,
)

urlpatterns = [
    path("me/", vendor_me, name="vendor-me"),
    path("dashboard/", vendor_dashboard, name="vendor-dashboard"),
    path("transactions/", vendor_transactions, name="vendor-transactions"),
    path("transactions.csv", vendor_transactions_csv,
         name="vendor-transactions-csv"),
    path("cards/", vendor_cards, name="vendor-cards"),
]
