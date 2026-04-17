"""
Partner management.

Partner:         a business partner entitled to a share of collected fees.
PartnerShare:    current % allocation for each partner (sum ≤ 100).
PartnerLedgerEntry: immutable record of every profit allocation per
                 transaction. Contains historical share snapshot so
                 later changes to PartnerShare don't rewrite history.
"""
import uuid
from decimal import Decimal
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


class Partner(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_partners",
    )

    class Meta:
        db_table = "partners"
        ordering = ["name"]

    def __str__(self):
        return self.name


class PartnerShare(models.Model):
    """Current share of each partner as a percentage (of total collected fee)."""
    partner = models.OneToOneField(
        Partner, on_delete=models.CASCADE, related_name="share", primary_key=True,
    )
    percentage = models.DecimalField(
        max_digits=6, decimal_places=3,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="Percentage of total collected fee (NOT of transaction amount).",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
    )

    class Meta:
        db_table = "partner_shares"

    def __str__(self):
        return f"{self.partner.name}: {self.percentage}%"


class PartnerLedgerEntry(models.Model):
    """
    Immutable profit allocation: one entry per (partner × transaction).
    Stores `share_snapshot` so historical reports are accurate even
    after shares are later re-balanced.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner = models.ForeignKey(
        Partner, on_delete=models.PROTECT, related_name="ledger_entries",
    )
    payment = models.ForeignKey(
        "myapp.IncomingPayment", on_delete=models.PROTECT,
        related_name="partner_ledger_entries",
    )

    # Historical snapshot
    share_snapshot = models.DecimalField(max_digits=6, decimal_places=3)
    fee_total_foreign = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text="Total fee collected (in the transaction's foreign currency).",
    )
    fee_total_pkr = models.DecimalField(max_digits=18, decimal_places=2)
    amount_foreign = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text="This partner's share of the foreign-currency fee.",
    )
    amount_pkr = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text="This partner's share of the fee in PKR.",
    )
    currency_code = models.CharField(max_length=3)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "partner_ledger_entries"
        ordering = ["-created_at"]
        unique_together = [("partner", "payment")]
        indexes = [
            models.Index(fields=["partner", "-created_at"]),
            models.Index(fields=["payment"]),
        ]

    def __str__(self):
        return f"{self.partner.name} ← {self.amount_pkr} PKR ({self.payment.reference})"
