"""Serializers for BankAlertEmail."""
from rest_framework import serializers
from myapp.Models.BankEmail_models import BankAlertEmail


class BankAlertEmailSerializer(serializers.ModelSerializer):
    matched_payment_reference = serializers.CharField(
        source="matched_payment.reference", read_only=True, default=None,
    )
    matched_payment_customer_name = serializers.CharField(
        source="matched_payment.customer.full_name", read_only=True, default=None,
    )
    matched_by_email = serializers.CharField(
        source="matched_by.email", read_only=True, default=None,
    )
    status_display = serializers.CharField(
        source="get_status_display", read_only=True,
    )

    class Meta:
        model = BankAlertEmail
        fields = [
            "id", "message_id", "sender_email", "subject", "received_at",
            "bank_type",
            "extracted_amount", "extracted_currency",
            "extracted_sender_name", "extracted_reference",
            "extraction_notes", "parser_error",
            "status", "status_display",
            "matched_payment", "matched_payment_reference",
            "matched_payment_customer_name",
            "conflict_payment_ids",
            "matched_by", "matched_by_email", "matched_at",
            "notes",
            "raw_body_text", "raw_body_html",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


class BankAlertEmailListSerializer(serializers.ModelSerializer):
    """Lighter version — no raw HTML body (which can be 100 KB+)."""
    matched_payment_reference = serializers.CharField(
        source="matched_payment.reference", read_only=True, default=None,
    )
    status_display = serializers.CharField(
        source="get_status_display", read_only=True,
    )

    class Meta:
        model = BankAlertEmail
        fields = [
            "id", "sender_email", "subject", "received_at",
            "bank_type",
            "extracted_amount", "extracted_currency",
            "extracted_sender_name", "extracted_reference",
            "status", "status_display",
            "matched_payment", "matched_payment_reference",
            "conflict_payment_ids",
            "notes", "created_at",
        ]
        read_only_fields = fields