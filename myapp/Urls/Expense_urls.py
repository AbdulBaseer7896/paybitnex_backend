"""Expense URLs."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from myapp.Views.Expense_views import ExpenseViewSet

router = DefaultRouter()
router.register(r"", ExpenseViewSet, basename="expenses")

urlpatterns = [
    path("", include(router.urls)),
]
