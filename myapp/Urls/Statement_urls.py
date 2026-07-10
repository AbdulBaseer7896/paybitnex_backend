"""Bank Statement URLs — the unified account-centric ledger."""
from django.urls import path

from myapp.Views.Statement_views import bank_statement

urlpatterns = [
    path("", bank_statement, name="bank-statement"),
]
