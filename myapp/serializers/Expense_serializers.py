"""Expense serializers — includes ExpenseDistribution support."""
from rest_framework import serializers
from myapp.Models.Expense_models import Expense, ExpenseDistribution


class ExpenseDistributionSerializer(serializers.ModelSerializer):
    partner_name = serializers.SerializerMethodField()
    is_company = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseDistribution
        fields = [
            "id", "partner", "partner_name", "is_company", "amount",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "partner_name", "is_company", "created_at", "updated_at"]

    def get_partner_name(self, obj):
        if obj.partner_id:
            return obj.partner.name if hasattr(obj, "partner") and obj.partner else None
        return "Company"

    def get_is_company(self, obj):
        return obj.partner_id is None


class ExpenseSerializer(serializers.ModelSerializer):
    currency_code = serializers.CharField(source="currency_id", read_only=True)
    created_by_email = serializers.CharField(source="created_by.email", read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    distributions = ExpenseDistributionSerializer(many=True, read_only=True)
    distribution_summary = serializers.SerializerMethodField()

    class Meta:
        model = Expense
        fields = [
            "id", "title", "category", "category_display", "vendor",
            "currency", "currency_code", "amount",
            "purpose", "spent_on", "document",
            "created_by", "created_by_email", "created_by_name",
            "created_at", "updated_at",
            "distributions", "distribution_summary",
        ]
        read_only_fields = [
            "id", "currency_code", "category_display",
            "created_by", "created_by_email", "created_by_name",
            "created_at", "updated_at",
            "distributions", "distribution_summary",
        ]

    def get_distribution_summary(self, obj):
        dists = list(obj.distributions.select_related("partner").all())
        if not dists:
            return None
        parts = []
        for d in dists:
            label = d.partner.name if d.partner_id else "Company"
            parts.append(f"{label}: {d.amount}")
        return " \u00b7 ".join(parts)


class ExpenseDistributionWriteSerializer(serializers.Serializer):
    partner = serializers.UUIDField(allow_null=True, required=False)
    amount = serializers.DecimalField(max_digits=18, decimal_places=2)


class ExpenseDistributionBulkSerializer(serializers.Serializer):
    distributions = serializers.ListField(
        child=ExpenseDistributionWriteSerializer(),
        min_length=1,
    )

    def validate_distributions(self, value):
        if not value:
            raise serializers.ValidationError("At least one distribution slice required.")
        return value
