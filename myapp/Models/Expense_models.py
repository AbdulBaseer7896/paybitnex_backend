"""
Company expenses — subscriptions, bank fees, office costs, anything the
business spends money on. Booked in any supported currency so we can
track PKR-denominated local bills alongside USD SaaS subscriptions, etc.
"""
import uuid
from django.db import models


class ExpenseCategory(models.TextChoices):
    SUBSCRIPTION = "subscription", "Subscription / SaaS"
    BANKING      = "banking",      "Bank fees / charges"
    PARTNER      = "partner",      "Partner payout"
    OFFICE       = "office",       "Office / operations"
    PAYROLL      = "payroll",      "Payroll / salaries"
    TAX          = "tax",          "Tax / regulatory"
    MARKETING    = "marketing",    "Marketing / advertising"
    OTHER        = "other",        "Other"


class Expense(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Categorization
    title = models.CharField(max_length=200)
    category = models.CharField(
        max_length=30, choices=ExpenseCategory.choices,
        default=ExpenseCategory.OTHER, db_index=True,
    )
    vendor = models.CharField(
        max_length=150, blank=True,
        help_text="Who was paid — e.g. 'Stripe', 'HBL', 'AWS'.",
    )

    # Amount
    currency = models.ForeignKey(
        "myapp.Currency", on_delete=models.PROTECT, to_field="code",
        related_name="expenses",
    )
    amount = models.DecimalField(max_digits=18, decimal_places=2)

    # Context
    purpose = models.TextField(
        blank=True,
        help_text="Free-form description of what this expense was for.",
    )
    spent_on = models.DateField(
        help_text="Date the expense was incurred (not the date it was entered).",
        db_index=True,
    )

    # Proof
    document = models.FileField(
        upload_to="expenses/docs/", null=True, blank=True,
        help_text="Receipt / invoice / screenshot — accepts images or PDFs.",
    )

    # Bookkeeping
    created_by = models.ForeignKey(
        "myapp.User", on_delete=models.PROTECT, related_name="created_expenses",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-spent_on", "-created_at"]
        indexes = [
            models.Index(fields=["spent_on", "category"]),
            models.Index(fields=["currency", "spent_on"]),
        ]

    def __str__(self):
        return f"{self.title} — {self.amount} {self.currency_id} on {self.spent_on}"
