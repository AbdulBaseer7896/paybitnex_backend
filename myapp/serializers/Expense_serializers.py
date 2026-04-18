"""Expense serializer."""
from rest_framework import serializers
from myapp.Models.Expense_models import Expense


class ExpenseSerializer(serializers.ModelSerializer):
    currency_code = serializers.CharField(source="currency_id", read_only=True)
    created_by_email = serializers.CharField(source="created_by.email", read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = Expense
        fields = [
            "id", "title", "category", "category_display", "vendor",
            "currency", "currency_code", "amount",
            "purpose", "spent_on", "document",
            "created_by", "created_by_email", "created_by_name",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "currency_code", "category_display",
            "created_by", "created_by_email", "created_by_name",
            "created_at", "updated_at",
        ]
