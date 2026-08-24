"""
Transaction flow (revised):
1. Customer submits IncomingPayment (USD only now; EUR/GBP deprecated).
   Customer picks a `payment_method` (Zelle / Cash App / ACH-Wire / custom).
2. Accountant VERIFIES documents → status becomes VERIFIED internally (but the
   customer-facing timeline treats verification as "Under processing").
3. Accountant applies rate → sets fee % → calculates net PKR.
4. Accountant records OutgoingPKRTransfer → status becomes PKR_SENT. At this
   point the customer sees the PKR transfer receipt and can click
   "I received my PKR" to mark COMPLETED.
5. If customer doesn't confirm within N days (see SystemSetting
   `stale_payment_days`, default 3), a daily Celery-beat task flags the
   payment `is_stale=True`. Stale payments are hidden from the main staff
   transactions list and appear in the "Awaiting customer confirmation"
   section, where admin can force-complete them.
6. Every state change logged in TransactionStatusHistory.

ALL amounts are Decimal. Never floats.
"""
import uuid
from decimal import Decimal
from django.db import models
from django.core.validators import MinValueValidator


class TransactionStatus(models.TextChoices):
    SUBMITTED = "submitted", "Submitted by Customer"
    UNDER_REVIEW = "under_review", "Under Review"
    VERIFIED = "verified", "Verified — Awaiting PKR Transfer"
    PKR_SENT = "pkr_sent", "PKR Sent to Customer"
    COMPLETED = "completed", "Completed"
    ON_HOLD = "on_hold", "On Hold"
    REJECTED = "rejected", "Rejected"


class IncomingPayment(models.Model):
    """Payment received by customer (USD). EUR/GBP retained only for legacy rows."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Human-readable reference e.g. "PBX-2026-000123"
    reference = models.CharField(max_length=40, unique=True, db_index=True)

    customer = models.ForeignKey(
        "myapp.User", on_delete=models.PROTECT, related_name="incoming_payments",
    )
    # Merchant account was removed — the customer's received-funds account is
    # no longer tracked on the payment itself. `merchant_account_id` column is
    # kept in the DB for legacy rows but the FK constraint is dropped via
    # migration (it's now just an opaque historical UUID string).

    # Payment method — string FK to PaymentMethod.code for resilience against
    # method additions/removals managed by admin in Settings.
    payment_method = models.ForeignKey(
        "myapp.PaymentMethod", on_delete=models.PROTECT, to_field="code",
        related_name="incoming_payments",
        null=True, blank=True,   # nullable so legacy rows (which predate this) still work
    )

    # Sender details (entered by customer). `sender_bank_name` and
    # `sender_account_last4` are kept as columns for historical rows but are
    # no longer collected on the New Payment form.
    sender_name = models.CharField(max_length=150)
    sender_company = models.CharField(max_length=150)
    sender_bank_name = models.CharField(max_length=150, blank=True)
    sender_account_last4 = models.CharField(max_length=10, blank=True)
    external_transaction_id = models.CharField(
        max_length=100, db_index=True,
        help_text="Unique ID from sender's bank / platform. "
                  "Rejected payments release their ID for reuse — "
                  "uniqueness is enforced at the serializer level (not DB).",
    )

    # Amount received
    currency = models.ForeignKey(
        "myapp.Currency", on_delete=models.PROTECT, to_field="code",
        related_name="incoming_payments",
    )
    amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    # Proof uploads (Cloudinary). `screenshot_email` kept as column for
    # historical rows but new submissions don't require it.
    screenshot_transaction = models.ImageField(upload_to="proofs/txn/")
    screenshot_email = models.ImageField(upload_to="proofs/email/", null=True, blank=True)
    extra_document = models.FileField(upload_to="proofs/docs/", null=True, blank=True)

    # ----- Verification step (BEFORE rate/fee) -----
    verified_note = models.TextField(
        blank=True,
        help_text="Short note the accountant writes when verifying documents.",
    )
    verified_document = models.FileField(
        upload_to="proofs/verification/", null=True, blank=True,
        help_text="Optional proof the accountant uploads while verifying.",
    )
    verified_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="verified_payments",
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    # Accountant-applied fields
    exchange_rate = models.DecimalField(
        max_digits=14, decimal_places=6, null=True, blank=True,
        help_text=(
            "Tangent rate: 1 unit of `currency` = X PKR — the rate "
            "given to the customer and used to compute net_pkr. "
            "Must never exceed real_exchange_rate."
        ),
    )
    real_exchange_rate = models.DecimalField(
        max_digits=14, decimal_places=6, null=True, blank=True,
        help_text=(
            "Actual market rate at time of transfer (admin-editable). "
            "Company rate-spread profit = (real - tangent) * total_amount."
        ),
    )
    # True while `exchange_rate` holds an auto-assigned PLACEHOLDER rate
    # (stamped at creation time by Utils/default_rate.py) rather than the
    # real rate an accountant negotiated. Flipped to False the moment a
    # human applies a rate via the accountant-apply endpoint.
    #
    # Reports use this to mark rows as estimated, so a week is never closed
    # on placeholder numbers by mistake. Fee-dependent columns (net_pkr,
    # fee_amount_foreign, net_amount_foreign) are left NULL while this is
    # True — a provisional rate never feeds profit or partner-ledger math.
    is_rate_provisional = models.BooleanField(
        default=False, db_index=True,
        help_text="True when exchange_rate is an auto-assigned placeholder "
                  "awaiting the accountant's real rate.",
    )
    # Fee allocation override for under-fee transactions (Update #3 fix)
    fee_allocation = models.JSONField(
        null=True, blank=True,
        help_text=(
            "Custom fee split when transaction fee < sum of partner shares. "
            'JSON: {"company": <pct_of_fee>, "partners": {"<uuid>": <pct_of_fee>}}'
        ),
    )
    fee_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Percentage charged as our fee",
    )
    fee_amount_foreign = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text="Fee in the received currency",
    )
    net_amount_foreign = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text="amount - fee_amount_foreign",
    )
    gross_pkr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text="amount * exchange_rate (before fee, reference only)",
    )
    net_pkr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text="net_amount_foreign * exchange_rate — what customer actually receives",
    )

    status = models.CharField(
        max_length=20, choices=TransactionStatus.choices,
        default=TransactionStatus.SUBMITTED, db_index=True,
    )
    accountant_notes = models.TextField(blank=True)
    accountant_document = models.FileField(
        upload_to="accountant/docs/", null=True, blank=True,
    )

    handled_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="handled_payments",
    )

    # ----- Customer confirmation + staleness tracking -----
    # Set when the customer clicks "I received my PKR" on their portal after
    # PKR_SENT. Triggers final COMPLETED status + partner fee distribution.
    customer_confirmed_at = models.DateTimeField(null=True, blank=True)

    # `is_stale` is toggled by the Celery-beat task `flag_stale_payments`
    # when a payment has been in PKR_SENT for longer than SystemSetting
    # `stale_payment_minutes` without the customer confirming. Stale payments
    # drop out of the main staff transactions list and appear in the
    # "Awaiting customer confirmation" section, where admin can force-complete.
    is_stale = models.BooleanField(default=False, db_index=True)

    # The moment `is_stale` flipped true, i.e. when this entered the awaiting-
    # confirmation queue. The auto-confirm timer is anchored here rather than
    # to `updated_at`, which any unrelated edit would push forward and so
    # silently restart the clock.
    stale_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text=(
            "When this payment was flagged stale and entered the "
            "Awaiting Customer Confirmation queue."
        ),
    )

    # Set when `auto_confirm_stale_payments` approved this on the customer's
    # behalf. Distinct from `force_completed_by`, which records a *human*
    # admin override — this one had no operator behind it, and the
    # distinction matters when auditing why a payment closed.
    auto_confirmed = models.BooleanField(
        default=False,
        help_text=(
            "True when the system approved this on the customer's "
            "behalf after the auto-confirm window elapsed."
        ),
    )

    # If admin force-completed this payment on behalf of an unresponsive
    # customer, track who did it (for the activity log).
    force_completed_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="force_completed_payments",
    )
    force_completed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Business / transaction date. `created_at` records when the row was
    # *entered* into the system; `occurred_on` is the date the payment
    # actually happened. Defaults to the entry date but staff (and the
    # batch entry grid) can override it — e.g. when an accountant records
    # a batch of payments a day or two after they were received. This is
    # the date shown to the customer as the official payment date.
    occurred_on = models.DateField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "incoming_payments"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "status"]),
            models.Index(fields=["currency", "created_at"]),
            models.Index(fields=["status", "is_stale"]),
        ]

    def __str__(self):
        return f"{self.reference} • {self.amount} {self.currency_id}"

    @property
    def is_verified(self):
        """True once an accountant has confirmed the uploaded documents."""
        return self.verified_at is not None

    @property
    def is_rate_fee_applied(self):
        """True once the accountant has filled exchange_rate AND fee_percentage.
        Required before any downstream step (PKR transfer, status change beyond
        UNDER_REVIEW). Used by permission checks in the views."""
        return self.exchange_rate is not None and self.fee_percentage is not None

    def calculate_amounts(self):
        """Recompute fee + net amounts (idempotent). Returns a dict of amounts.
        
        exchange_rate = tangent/customer rate (what customer sees)
        real_exchange_rate = actual market rate (company keeps the spread)
        
        Customer receives: net_amount_foreign * exchange_rate (tangent)
        Company rate-spread profit:
            (real_exchange_rate - exchange_rate) * amount  (on customer's net)
        """
        if self.exchange_rate is None or self.fee_percentage is None:
            return None
        fee_pct = self.fee_percentage / Decimal("100")
        self.fee_amount_foreign = (self.amount * fee_pct).quantize(Decimal("0.01"))
        self.net_amount_foreign = (self.amount - self.fee_amount_foreign).quantize(Decimal("0.01"))
        self.gross_pkr = (self.amount * self.exchange_rate).quantize(Decimal("0.01"))
        self.net_pkr = (self.net_amount_foreign * self.exchange_rate).quantize(Decimal("0.01"))
        return {
            "fee_amount_foreign": self.fee_amount_foreign,
            "net_amount_foreign": self.net_amount_foreign,
            "gross_pkr": self.gross_pkr,
            "net_pkr": self.net_pkr,
        }

    def compute_rate_spread_profit(self):
        """
        Company profit purely from the exchange rate spread (internal only).
        
        = (real_exchange_rate - exchange_rate) * amount
        
        This is the hidden profit from the rate difference. Both the customer's
        net amount AND the fee portions earn this spread for the company.
        Partners only receive their fee portion at the tangent rate — the spread
        on partner amounts also stays with the company.
        
        Returns Decimal in PKR, or Decimal('0') if real_exchange_rate not set.
        """
        if not self.real_exchange_rate or not self.exchange_rate:
            return Decimal("0")
        spread = Decimal(str(self.real_exchange_rate)) - Decimal(str(self.exchange_rate))
        if spread <= 0:
            return Decimal("0")
        return (Decimal(str(self.amount)) * spread).quantize(Decimal("0.01"))


class OutgoingPKRTransfer(models.Model):
    """PKR transfer sent by accountant to customer's PK bank account."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=40, unique=True, db_index=True)

    incoming_payment = models.OneToOneField(
        IncomingPayment, on_delete=models.PROTECT,
        related_name="outgoing_transfer",
        null=True, blank=True,
    )
    # A single PKR transfer can settle MANY of a customer's payments at once
    # (the company often sends one lump sum covering several USD receipts).
    # `incoming_payment` above is kept for backward-compatibility with the
    # single-payment flow and historical rows; new bulk transfers populate
    # this M2M instead. All linked payments must belong to the same customer.
    payments = models.ManyToManyField(
        IncomingPayment,
        related_name="covering_transfers",
        blank=True,
    )
    customer_bank_account = models.ForeignKey(
        "myapp.CustomerBankAccount", on_delete=models.PROTECT,
        related_name="pkr_transfers",
    )

    amount_pkr = models.DecimalField(max_digits=18, decimal_places=2)
    bank_transaction_id = models.CharField(
        max_length=100, help_text="Reference from our bank",
    )
    receipt = models.ImageField(upload_to="transfers/receipts/", null=True, blank=True)
    notes = models.TextField(blank=True)

    sent_by = models.ForeignKey(
        "myapp.User", on_delete=models.PROTECT, related_name="sent_transfers",
    )
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "outgoing_pkr_transfers"
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.reference} • PKR {self.amount_pkr}"


class TransactionStatusHistory(models.Model):
    """Append-only log of every status transition."""
    id = models.BigAutoField(primary_key=True)
    payment = models.ForeignKey(
        IncomingPayment, on_delete=models.CASCADE, related_name="status_history",
    )
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "transaction_status_history"
        ordering = ["-created_at"]
