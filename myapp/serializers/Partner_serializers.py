"""Partner + share + ledger serializers."""
from rest_framework import serializers
from decimal import Decimal

from myapp.Models.Partner_models import Partner, PartnerShare, PartnerLedgerEntry


class PartnerShareSerializer(serializers.ModelSerializer):
    class Meta:
        model = PartnerShare
        fields = ["partner", "percentage", "updated_at"]
        read_only_fields = ["partner", "updated_at"]


class PartnerSerializer(serializers.ModelSerializer):
    share_percentage = serializers.SerializerMethodField()

    class Meta:
        model = Partner
        fields = [
            "id", "name", "email", "phone", "notes", "is_active",
            "share_percentage",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "share_percentage"]

    def get_share_percentage(self, obj):
        share = getattr(obj, "share", None)
        return str(share.percentage) if share else "0.000"


class PartnerCreateSerializer(serializers.ModelSerializer):
    """Create a partner and optionally set initial share percentage."""
    share_percentage = serializers.DecimalField(
        max_digits=6, decimal_places=3, required=False, default=Decimal("0"),
    )

    class Meta:
        model = Partner
        fields = [
            "id", "name", "email", "phone", "notes", "is_active",
            "share_percentage",
        ]
        read_only_fields = ["id"]


class PartnerLedgerEntrySerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source="partner.name", read_only=True)
    payment_reference = serializers.CharField(source="payment.reference", read_only=True)
    # Customer details on the underlying payment — exposed so the Partner
    # Detail page can show a per-customer breakdown in its report without
    # a second round-trip per row.
    customer_id = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    customer_email = serializers.SerializerMethodField()
    payment_amount = serializers.SerializerMethodField()

    class Meta:
        model = PartnerLedgerEntry
        fields = [
            "id",
            "partner", "partner_name",
            "payment", "payment_reference",
            "customer_id", "customer_name", "customer_email",
            "payment_amount",
            "share_snapshot",
            "fee_total_foreign", "fee_total_pkr",
            "amount_foreign", "amount_pkr", "currency_code",
            "real_exchange_rate_snapshot", "rate_spread_profit_pkr",
            "created_at",
        ]
        read_only_fields = fields

    def get_customer_id(self, obj):
        return str(obj.payment.customer_id) if obj.payment_id else None

    def get_customer_name(self, obj):
        cust = getattr(obj.payment, "customer", None)
        if not cust:
            return None
        return cust.full_name or cust.email or str(cust.id)

    def get_customer_email(self, obj):
        cust = getattr(obj.payment, "customer", None)
        return cust.email if cust else None

    def get_payment_amount(self, obj):
        pay = obj.payment
        return {
            "amount": str(pay.amount),
            "currency": pay.currency_id,
            "net_pkr": str(pay.net_pkr) if pay.net_pkr is not None else None,
        }


class SharesBulkUpdateSerializer(serializers.Serializer):
    """Admin re-balances all partner shares in one atomic call."""
    shares = serializers.ListField(
        child=serializers.DictField(), min_length=1,
        help_text='Each item: {"partner": <uuid>, "percentage": "5.000"}',
    )

    def validate_shares(self, value):
        total = Decimal("0")
        seen = set()
        for item in value:
            if "partner" not in item or "percentage" not in item:
                raise serializers.ValidationError(
                    "Each entry requires 'partner' and 'percentage'."
                )
            if item["partner"] in seen:
                raise serializers.ValidationError("Duplicate partner in list.")
            seen.add(item["partner"])
            try:
                pct = Decimal(str(item["percentage"]))
            except Exception:
                raise serializers.ValidationError("Invalid percentage value.")
            if pct < 0 or pct > 100:
                raise serializers.ValidationError("Percentage must be 0..100.")
            total += pct
        if total > Decimal("100"):
            raise serializers.ValidationError(
                f"Total shares exceed 100% (got {total})."
            )
        return value
