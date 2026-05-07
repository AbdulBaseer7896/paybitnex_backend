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


class ExpenseDistribution(models.Model):
    """
    Tracks how an expense is split between partners and the company.

    One row per slice. NULL partner_id = company slice.
    All slices for an expense should sum to the expense's total amount,
    but we don't enforce this at DB level — the API validates it.

    Example: $50 expense split 40 company + 10 partner A:
        ExpenseDistribution(expense=e, partner=None,      amount=40)
        ExpenseDistribution(expense=e, partner=partner_a, amount=10)
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    expense = models.ForeignKey(
        "myapp.Expense", on_delete=models.CASCADE, related_name="distributions",
    )
    # NULL = company slice
    partner = models.ForeignKey(
        "myapp.Partner", on_delete=models.PROTECT,
        null=True, blank=True, related_name="expense_distributions",
        help_text="Null means this slice belongs to the company.",
    )
    amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text="Amount in the expense's currency assigned to this slice.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="expense_distribution_updates",
    )

    class Meta:
        db_table = "expense_distributions"
        ordering = ["partner__name"]
        indexes = [
            models.Index(fields=["expense"], name="exp_dist_expense_idx"),
            models.Index(fields=["partner"], name="exp_dist_partner_idx"),
        ]

    def __str__(self):
        who = self.partner.name if self.partner_id else "Company"
        return f"{who}: {self.amount} ({self.expense_id})"
