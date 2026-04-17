"""Exchange rate serializers."""
from rest_framework import serializers
from myapp.Models.Rate_models import ExchangeRate, ExchangeRateHistory


class ExchangeRateSerializer(serializers.ModelSerializer):
    currency_code = serializers.CharField(source="currency_id", read_only=True)
    currency_name = serializers.CharField(source="currency.name", read_only=True)
    currency_symbol = serializers.CharField(source="currency.symbol", read_only=True)

    class Meta:
        model = ExchangeRate
        fields = [
            "currency", "currency_code", "currency_name", "currency_symbol",
            "rate_to_pkr", "source", "manual_override_until",
            "updated_at",
        ]
        read_only_fields = ["currency_code", "currency_name", "currency_symbol", "updated_at"]


class ExchangeRateHistorySerializer(serializers.ModelSerializer):
    set_by_email = serializers.CharField(source="set_by.email", read_only=True, default=None)

    class Meta:
        model = ExchangeRateHistory
        fields = [
            "id", "currency_code", "rate_to_pkr",
            "source", "set_by", "set_by_email", "created_at",
        ]
        read_only_fields = fields


class ManualRateOverrideSerializer(serializers.Serializer):
    """Admin/accountant overrides a rate."""
    currency = serializers.CharField()
    rate_to_pkr = serializers.DecimalField(max_digits=14, decimal_places=6)
    override_hours = serializers.IntegerField(
        required=False, min_value=1, max_value=24 * 30,
        help_text="How many hours before live fetches resume. Default 24.",
        default=24,
    )
