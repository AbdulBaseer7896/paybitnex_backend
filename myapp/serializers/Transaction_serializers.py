"""
Transaction serializers:
- IncomingPaymentCreateSerializer: customer submits a payment
- IncomingPaymentSerializer: full detail (for customer history + accountant queue)
- PaymentVerifySerializer: accountant verifies the proofs (note + optional pic)
- AccountantApplySerializer: accountant sets rate + fee + marks verified
- OutgoingTransferSerializer: accountant records the PKR transfer
"""
from rest_framework import serializers

from myapp.Models.Transaction_models import (
    IncomingPayment, OutgoingPKRTransfer, TransactionStatusHistory,
)


class IncomingPaymentCreateSerializer(serializers.ModelSerializer):
    """What the customer submits when adding a new payment."""
    class Meta:
        model = IncomingPayment
        fields = [
            "merchant_account",
            "sender_name", "sender_company",
            "sender_bank_name", "sender_account_last4",
            "external_transaction_id",
            "currency", "amount",
            "screenshot_transaction", "screenshot_email",
            "extra_document",
        ]


class TransactionStatusHistorySerializer(serializers.ModelSerializer):
    changed_by_email = serializers.CharField(source="changed_by.email", read_only=True)
    changed_by_name = serializers.CharField(source="changed_by.full_name", read_only=True)

    class Meta:
        model = TransactionStatusHistory
        fields = [
            "id", "from_status", "to_status",
            "changed_by", "changed_by_email", "changed_by_name",
            "note", "created_at",
        ]
        read_only_fields = fields


class IncomingPaymentSerializer(serializers.ModelSerializer):
    currency_code = serializers.CharField(source="currency_id", read_only=True)
    customer_email = serializers.CharField(source="customer.email", read_only=True)
    customer_name = serializers.CharField(source="customer.full_name", read_only=True)
    handled_by_email = serializers.CharField(
        source="handled_by.email", read_only=True, default=None,
    )
    verified_by_email = serializers.CharField(
        source="verified_by.email", read_only=True, default=None,
    )
    verified_by_name = serializers.CharField(
        source="verified_by.full_name", read_only=True, default=None,
    )
    is_verified = serializers.BooleanField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    status_history = TransactionStatusHistorySerializer(many=True, read_only=True)

    class Meta:
        model = IncomingPayment
        fields = [
            "id", "reference",
            "customer", "customer_email", "customer_name",
            "merchant_account",
            "sender_name", "sender_company",
            "sender_bank_name", "sender_account_last4",
            "external_transaction_id",
            "currency", "currency_code", "amount",
            "screenshot_transaction", "screenshot_email", "extra_document",
            # verification stage
            "is_verified",
            "verified_note", "verified_document",
            "verified_by", "verified_by_email", "verified_by_name",
            "verified_at",
            # rate / fee stage
            "exchange_rate", "fee_percentage",
            "fee_amount_foreign", "net_amount_foreign",
            "gross_pkr", "net_pkr",
            "status", "status_display",
            "accountant_notes", "accountant_document",
            "handled_by", "handled_by_email",
            "created_at", "updated_at", "completed_at",
            "status_history",
        ]
        read_only_fields = [
            "id", "reference", "customer",
            "currency_code", "customer_email", "customer_name",
            "handled_by_email", "status_display",
            "is_verified",
            "verified_by_email", "verified_by_name",
            "fee_amount_foreign", "net_amount_foreign", "gross_pkr", "net_pkr",
            "handled_by", "status_history",
            "created_at", "updated_at", "verified_at", "completed_at",
        ]


class PaymentVerifySerializer(serializers.Serializer):
    """
    Stage 1: the accountant reviews the customer's uploaded proofs, writes a
    short note, optionally attaches their own confirmation pic, and marks the
    payment as verified — which unlocks the Apply-Rate-Fee stage.
    """
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    document = serializers.FileField(required=False, allow_null=True)


class AccountantApplySerializer(serializers.Serializer):
    """
    Accountant applies rate + fee + optionally marks verified.
    Net amounts are (re)calculated automatically.
    """
    exchange_rate = serializers.DecimalField(max_digits=14, decimal_places=6)
    fee_percentage = serializers.DecimalField(max_digits=5, decimal_places=2)
    accountant_notes = serializers.CharField(required=False, allow_blank=True)
    mark_verified = serializers.BooleanField(default=False)


class OutgoingTransferCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = OutgoingPKRTransfer
        fields = [
            "incoming_payment", "customer_bank_account",
            "amount_pkr", "bank_transaction_id",
            "receipt", "notes",
        ]


class OutgoingTransferSerializer(serializers.ModelSerializer):
    sent_by_email = serializers.CharField(source="sent_by.email", read_only=True)

    class Meta:
        model = OutgoingPKRTransfer
        fields = [
            "id", "reference",
            "incoming_payment", "customer_bank_account",
            "amount_pkr", "bank_transaction_id",
            "receipt", "notes",
            "sent_by", "sent_by_email", "sent_at",
        ]
        read_only_fields = [
            "id", "reference", "sent_by", "sent_by_email", "sent_at",
        ]


class StatusUpdateSerializer(serializers.Serializer):
    """Generic status transition: used for reject / hold / manual updates."""
    status = serializers.CharField()
    note = serializers.CharField(required=False, allow_blank=True)
