"""
Transaction serializers (revised for new flow):
- IncomingPaymentCreateSerializer: customer submits a payment (simplified).
- IncomingPaymentSerializer: full detail, now exposes `payment_method`,
  `is_rate_fee_applied`, `customer_confirmed_at`, `is_stale`, etc.
- PaymentVerifySerializer: accountant verifies the proofs.
- AccountantApplySerializer: accountant sets rate + fee.
- OutgoingTransferSerializer: accountant records the PKR transfer.
- CustomerConfirmSerializer / ForceCompleteSerializer: new completion flow.
"""
from myapp.Utils.file_validators import validate_image_file, validate_doc_file
from decimal import Decimal
from rest_framework import serializers

from myapp.Models.Transaction_models import (
    IncomingPayment, OutgoingPKRTransfer, TransactionStatusHistory,
)
from myapp.Models.Core_models import PaymentMethod

# Sentinel so a memoised `None` (genuinely no transfer) is distinguishable
# from "not looked up yet" — otherwise every no-transfer row re-resolves.
_UNRESOLVED = object()


class PaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentMethod
        fields = ["code", "label", "method_type",
                  "is_active", "is_default", "sort_order"]


class IncomingPaymentCreateSerializer(serializers.ModelSerializer):
    """
    What the customer submits when adding a new payment (simplified).

    Removed vs. old flow:
    - `merchant_account` (no longer collected — merchant accounts deprecated)
    - `sender_bank_name` / `sender_account_last4` (optional noise)
    - `screenshot_email` (optional email-confirmation proof — no longer collected)

    Added:
    - `payment_method` — required; one of the active PaymentMethod codes
      (Zelle, Cash App, ACH-Wire, or whatever admin added in Settings).
    """
    payment_method = serializers.SlugRelatedField(
        slug_field="code",
        queryset=PaymentMethod.objects.filter(is_active=True),
        required=True,
    )
    # Currency is resolved at call-time via __init__ below so we don't
    # import the Currency model at module-load time (avoids circular imports
    # in certain Django apps bootstrapping orders).
    currency = serializers.CharField(required=False, allow_blank=True)
    # Business date of the payment. Optional — defaults to today (set in the
    # view) when omitted. Lets staff / batch entry backdate a transaction.
    occurred_on = serializers.DateField(required=False, allow_null=True)

    class Meta:
        model = IncomingPayment
        fields = [
            "payment_method",
            "sender_name", "sender_company",
            "external_transaction_id",
            "currency", "amount",
            "screenshot_transaction",
            "extra_document",
            "occurred_on",
        ]
        extra_kwargs = {
            "screenshot_transaction": {"validators": [validate_image_file]},
            "extra_document": {"validators": [validate_doc_file]},
            # Remove Django's auto UniqueValidator for external_transaction_id.
            # Our custom validate_external_transaction_id() handles this with
            # rejected-payment awareness — it allows reuse of IDs from rejected txns.
            "external_transaction_id": {"validators": []},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def validate(self, attrs):
        # Default currency to USD if the client didn't send one, otherwise
        # resolve the code string to a Currency row. Handled manually (instead
        # of via SlugRelatedField) so the model load order is simpler.
        from myapp.Models.Core_models import Currency
        code = (attrs.get("currency") or "").strip() or "USD"
        try:
            attrs["currency"] = Currency.objects.get(code=code, is_active=True)
        except Currency.DoesNotExist:
            raise serializers.ValidationError(
                {"currency": f"'{code}' is not a supported currency."}
            )

        # Validate the payment method against the customer's allowed
        # methods. Admins manage these via CustomerAllowedPaymentMethod;
        # if a customer has any rows in that table, they MUST pick one
        # of their granted methods. If they have zero rows, we fall
        # back to allowing any active method (unrestricted access —
        # matches the legacy behavior for customers who pre-date the
        # allowed-methods feature). Staff users (admin/accountant)
        # bypass this check entirely since they create transactions
        # on behalf of customers via different flows.
        request = self.context.get("request")
        method = attrs.get("payment_method")
        if request and method and getattr(request.user, "role", None) == "customer":
            from myapp.Models.Invoicing_models import CustomerAllowedPaymentMethod
            grant_codes = list(CustomerAllowedPaymentMethod.objects.filter(
                customer=request.user,
            ).values_list("payment_method_id", flat=True))
            if grant_codes and method.code not in grant_codes:
                raise serializers.ValidationError(
                    {"payment_method":
                        f"You're not approved to receive payments via "
                        f"{method.label}. Please contact support."}
                )
        return attrs

    def validate_external_transaction_id(self, value):
        if not value:
            return value
        # Allow reuse of external IDs from REJECTED payments — they are considered
        # released and can be resubmitted by the customer.
        from myapp.Models.Transaction_models import TransactionStatus
        existing = IncomingPayment.objects.filter(external_transaction_id=value)
        # Exclude the current instance in update scenarios
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        # Only block if there is a non-rejected payment with this ID
        if existing.exclude(status=TransactionStatus.REJECTED).exists():
            raise serializers.ValidationError(
                "Incoming payment with this Confirmation Code already exists."
            )
        return value

    def validate_sender_company(self, value):
        if not value or not str(value).strip():
            raise serializers.ValidationError("Sender company is required.")
        return value


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
    force_completed_by_email = serializers.CharField(
        source="force_completed_by.email", read_only=True, default=None,
    )
    is_verified = serializers.BooleanField(read_only=True)
    is_rate_fee_applied = serializers.BooleanField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    status_history = TransactionStatusHistorySerializer(many=True, read_only=True)

    payment_method_code = serializers.CharField(
        source="payment_method_id", read_only=True,
    )
    payment_method_label = serializers.CharField(
        source="payment_method.label", read_only=True, default=None,
    )

    has_pkr_transfer = serializers.SerializerMethodField()
    transfer_receipt = serializers.SerializerMethodField()
    transfer_notes = serializers.SerializerMethodField()
    transfer_bank_transaction_id = serializers.SerializerMethodField()
    transfer_amount_pkr = serializers.SerializerMethodField()
    transfer_recorded_at = serializers.SerializerMethodField()
    transfer_recorded_by_email = serializers.SerializerMethodField()

    # Rate spread profit (company internal — show only to admin/accountant)
    rate_spread_profit_pkr = serializers.SerializerMethodField()

    def get_rate_spread_profit_pkr(self, obj):
        """Company profit from rate spread = (real - tangent) * amount."""
        try:
            return str(obj.compute_rate_spread_profit())
        except Exception:
            return "0.00"

    def get_has_pkr_transfer(self, obj):
        return self._transfer(obj) is not None

    def _transfer(self, obj):
        """Internal helper — returns the related OutgoingTransfer or None.

        Prefers the single-payment transfer (legacy/standard flow); falls
        back to the most recent bulk transfer covering this payment so the
        customer sees the shared bank txn ID + receipt on every payment the
        lump-sum settled.

        PERFORMANCE — this is why the admin dashboard used to take 11s:
        seven fields on this serializer need the transfer, and each one used
        to run its own `covering_transfers.order_by(...).first()` /
        `.exists()`. At 500 rows that was ~3,500 queries. Two changes fix it:

          1. The result is memoised on the instance, so the seven fields
             share ONE resolution.
          2. It reads `covering_transfers.all()`, which consumes the
             `Prefetch` set up in IncomingPaymentViewSet (already ordered by
             -sent_at) instead of issuing a fresh ordered query. Calling
             `.order_by()` or `.exists()` here would bypass the prefetch and
             silently reintroduce the N+1 — don't.
        """
        cached = getattr(obj, "_resolved_transfer", _UNRESOLVED)
        if cached is not _UNRESOLVED:
            return cached

        resolved = getattr(obj, "outgoing_transfer", None)
        if resolved is None:
            try:
                covering = list(obj.covering_transfers.all())
                resolved = covering[0] if covering else None
            except Exception:
                resolved = None

        obj._resolved_transfer = resolved
        return resolved

    def get_transfer_receipt(self, obj):
        t = self._transfer(obj)
        if not t or not t.receipt:
            return None
        # Return absolute URL if we have a request context, else the path
        request = self.context.get("request")
        try:
            return request.build_absolute_uri(t.receipt.url) if request else t.receipt.url
        except Exception:
            return None

    def get_transfer_notes(self, obj):
        t = self._transfer(obj)
        return t.notes if t else None

    def get_transfer_bank_transaction_id(self, obj):
        t = self._transfer(obj)
        return t.bank_transaction_id if t else None

    def get_transfer_amount_pkr(self, obj):
        t = self._transfer(obj)
        return str(t.amount_pkr) if t else None

    def get_transfer_recorded_at(self, obj):
        t = self._transfer(obj)
        return t.sent_at.isoformat() if t and t.sent_at else None

    def get_transfer_recorded_by_email(self, obj):
        t = self._transfer(obj)
        return getattr(t.sent_by, "email", None) if t and t.sent_by else None

    class Meta:
        model = IncomingPayment
        fields = [
            "id", "reference",
            "customer", "customer_email", "customer_name",
            "payment_method", "payment_method_code", "payment_method_label",
            "sender_name", "sender_company",
            "sender_bank_name", "sender_account_last4",
            "external_transaction_id",
            "currency", "currency_code", "amount",
            "screenshot_transaction", "screenshot_email", "extra_document",
            "is_verified",
            "verified_note", "verified_document",
            "verified_by", "verified_by_email", "verified_by_name",
            "verified_at",
            "exchange_rate", "real_exchange_rate", "fee_allocation",
            "is_rate_provisional",
            "fee_percentage",
            "fee_amount_foreign", "net_amount_foreign",
            "gross_pkr", "net_pkr",
            "rate_spread_profit_pkr",
            "is_rate_fee_applied",
            "status", "status_display",
            "accountant_notes", "accountant_document",
            "handled_by", "handled_by_email",
            "customer_confirmed_at", "is_stale",
            "force_completed_by", "force_completed_by_email", "force_completed_at",
            "occurred_on",
            "created_at", "updated_at", "completed_at",
            "status_history",
            "has_pkr_transfer",
            "transfer_receipt", "transfer_notes", "transfer_bank_transaction_id",
            "transfer_amount_pkr", "transfer_recorded_at", "transfer_recorded_by_email",
        ]
        read_only_fields = [
            "id", "reference", "customer",
            "currency_code", "customer_email", "customer_name",
            "handled_by_email", "status_display",
            "is_verified", "is_rate_fee_applied", "is_rate_provisional",
            "verified_by_email", "verified_by_name",
            "fee_amount_foreign", "net_amount_foreign", "gross_pkr", "net_pkr",
            "rate_spread_profit_pkr",
            "handled_by", "status_history",
            "payment_method_code", "payment_method_label",
            "customer_confirmed_at", "is_stale",
            "force_completed_by_email", "force_completed_at",
            "has_pkr_transfer",
            "transfer_receipt", "transfer_notes", "transfer_bank_transaction_id",
            "transfer_amount_pkr", "transfer_recorded_at", "transfer_recorded_by_email",
            "occurred_on",
            "created_at", "updated_at", "verified_at", "completed_at",
        ]


class PaymentVerifySerializer(serializers.Serializer):
    """Stage 1: accountant reviews the customer's proofs + marks verified."""
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    document = serializers.FileField(required=False, allow_null=True)


class AccountantApplySerializer(serializers.Serializer):
    """Accountant applies rate + fee; net amounts computed automatically.
    
    exchange_rate = tangent/customer rate (what customer sees)
    real_exchange_rate = actual market rate (admin can edit later; default = exchange_rate)
    fee_allocation = optional override for under-fee transactions
    """
    exchange_rate = serializers.DecimalField(max_digits=14, decimal_places=6)
    real_exchange_rate = serializers.DecimalField(
        max_digits=14, decimal_places=6, required=False, allow_null=True,
        help_text="Actual market rate. Defaults to exchange_rate if not provided.",
    )
    fee_percentage = serializers.DecimalField(max_digits=5, decimal_places=2)
    fee_allocation = serializers.JSONField(
        required=False, allow_null=True,
        help_text=(
            "Custom fee split for under-fee transactions. "
            '{"company": <pct>, "partners": {"<uuid>": <pct>}}'
        ),
    )
    accountant_notes = serializers.CharField(required=False, allow_blank=True)
    mark_verified = serializers.BooleanField(default=False)

    def validate(self, attrs):
        tangent = attrs.get("exchange_rate")
        real = attrs.get("real_exchange_rate")
        if tangent and real and real < tangent:
            raise serializers.ValidationError({
                "real_exchange_rate": (
                    "Actual rate cannot be less than the customer (tangent) rate. "
                    "The customer rate is what they receive — it must be ≤ actual rate."
                )
            })
        # Default real_exchange_rate to exchange_rate if not provided
        if tangent and not real:
            attrs["real_exchange_rate"] = tangent
        return attrs


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
    payment_ids = serializers.SerializerMethodField()

    def get_payment_ids(self, obj):
        # All payments this transfer covers: the legacy single FK (if any)
        # plus the M2M set, de-duplicated.
        ids = set()
        if obj.incoming_payment_id:
            ids.add(str(obj.incoming_payment_id))
        for p in obj.payments.all():
            ids.add(str(p.id))
        return list(ids)

    class Meta:
        model = OutgoingPKRTransfer
        fields = [
            "id", "reference",
            "incoming_payment", "payment_ids", "customer_bank_account",
            "amount_pkr", "bank_transaction_id",
            "receipt", "notes",
            "sent_by", "sent_by_email", "sent_at",
        ]
        read_only_fields = [
            "id", "reference", "payment_ids",
            "sent_by", "sent_by_email", "sent_at",
        ]


class OutgoingTransferBulkCreateSerializer(serializers.Serializer):
    """Record ONE PKR transfer that settles many of a customer's payments.

    The company frequently sends a single lump-sum PKR payment covering
    several of a customer's USD receipts. This validates that every
    selected payment belongs to the same customer, shares one currency,
    and is ready (rate+fee applied, not already settled).
    """
    payment_ids = serializers.ListField(
        child=serializers.UUIDField(), allow_empty=False,
    )
    # Accept the bank-account id as a UUID and resolve it in validate() so
    # we don't need a relational field with an eager queryset at import time
    # (which would assert at class-definition before any __init__ runs).
    customer_bank_account = serializers.UUIDField()
    amount_pkr = serializers.DecimalField(max_digits=18, decimal_places=2)
    bank_transaction_id = serializers.CharField(max_length=100)
    receipt = serializers.ImageField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_customer_bank_account(self, value):
        from myapp.Models.Banking_models import CustomerBankAccount
        try:
            return CustomerBankAccount.objects.get(pk=value)
        except CustomerBankAccount.DoesNotExist:
            raise serializers.ValidationError("Unknown bank account.")


class StatusUpdateSerializer(serializers.Serializer):
    """Generic status transition: used for reject / hold / manual updates."""
    status = serializers.CharField()
    note = serializers.CharField(required=False, allow_blank=True)


class CustomerConfirmSerializer(serializers.Serializer):
    """Customer clicking 'I received my PKR'. Optional note."""
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)


class ForceCompleteSerializer(serializers.Serializer):
    """Admin force-completes an unresponsive-customer stale payment."""
    reason = serializers.CharField(required=True, max_length=500)


class UpdateRealRateSerializer(serializers.Serializer):
    """Admin/accountant updates the real (actual) exchange rate on a transaction.
    Only real_exchange_rate is editable post-transfer.
    """
    real_exchange_rate = serializers.DecimalField(
        max_digits=14, decimal_places=6,
        help_text="Actual market rate (must be >= customer/tangent rate).",
    )


class FeeAllocationSerializer(serializers.Serializer):
    """Validate fee allocation for under-fee transactions (Update #3)."""
    # NOTE: DRF wants Decimal instances (not ints) for min_value/max_value
    # on DecimalField — ints work but emit a UserWarning on every startup.
    company = serializers.DecimalField(
        max_digits=6, decimal_places=3,
        min_value=Decimal("0"), max_value=Decimal("100"),
    )
    partners = serializers.DictField(
        child=serializers.DecimalField(max_digits=6, decimal_places=3,
                                       min_value=Decimal("0"),
                                       max_value=Decimal("100")),
        required=False,
        help_text="{'<partner_uuid>': <percentage_of_fee>}",
    )

    def validate(self, attrs):
        company_pct = attrs.get("company", 0)
        partners = attrs.get("partners", {})
        total = company_pct + sum(partners.values())
        if abs(total - 100) > 0.01:
            raise serializers.ValidationError(
                f"Company + all partner percentages must sum to 100 (got {total})."
            )
        return attrs
