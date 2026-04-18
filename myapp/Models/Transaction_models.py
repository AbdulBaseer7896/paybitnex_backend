"""
Transaction flow:
1. Customer submits IncomingPayment (USD/EUR/GBP received in their merchant account).
2. Accountant VERIFIES documents → saves verification note + optional pic (status → under_review/verified).
3. Accountant applies rate → sets fee % → calculates net PKR.
4. Accountant creates OutgoingPKRTransfer → marks transaction complete.
5. Every state change logged in TransactionStatusHistory.

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
    """Payment received by customer in foreign currency (USD/EUR/GBP)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Human-readable reference e.g. "PBX-2026-000123"
    reference = models.CharField(max_length=40, unique=True, db_index=True)

    customer = models.ForeignKey(
        "myapp.User", on_delete=models.PROTECT, related_name="incoming_payments",
    )
    merchant_account = models.ForeignKey(
        "myapp.CustomerMerchantAccount", on_delete=models.PROTECT,
        related_name="incoming_payments",
    )

    # Sender details (entered by customer)
    sender_name = models.CharField(max_length=150)
    sender_company = models.CharField(max_length=150)
    sender_bank_name = models.CharField(max_length=150, blank=True)
    sender_account_last4 = models.CharField(max_length=10, blank=True)
    external_transaction_id = models.CharField(
        max_length=100, unique=True, db_index=True,
        help_text="Unique ID from sender's bank / platform — unique across all transactions",
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

    # Proof uploads (Cloudinary)
    screenshot_transaction = models.ImageField(upload_to="proofs/txn/")
    screenshot_email = models.ImageField(upload_to="proofs/email/", null=True, blank=True)
    extra_document = models.FileField(upload_to="proofs/docs/", null=True, blank=True)

    # ----- Verification step (BEFORE rate/fee) -----
    # The accountant confirms the uploaded proofs are genuine. This is a
    # separate stage from applying rate + fee — completed verifications are
    # immutable and appear as an audit item on the payment.
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
        help_text="1 unit of `currency` = X PKR",
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

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "incoming_payments"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "status"]),
            models.Index(fields=["currency", "created_at"]),
        ]

    def __str__(self):
        return f"{self.reference} • {self.amount} {self.currency_id}"

    @property
    def is_verified(self):
        """True once an accountant has confirmed the uploaded documents."""
        return self.verified_at is not None

    def calculate_amounts(self):
        """Recompute fee + net amounts (idempotent). Returns a dict of amounts."""
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


class OutgoingPKRTransfer(models.Model):
    """PKR transfer sent by accountant to customer's PK bank account."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=40, unique=True, db_index=True)

    incoming_payment = models.OneToOneField(
        IncomingPayment, on_delete=models.PROTECT,
        related_name="outgoing_transfer",
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
