"""
Bank-reconciliation audit module.

This is SEPARATE from the action-log `AuditLog` in `Audit_models.py`.
Here we reconcile the company's recorded money movement (customer
`IncomingPayment` rows + company `InternalTransaction` rows) against the
statement files (CSV / Excel) the banks hand us.

Workflow:
  1. Admin picks a bank (cashapp / amex / us_bank / generic) and a date
     range, then uploads that bank's statement file.
  2. The backend parses the file using the per-bank column mapping,
     normalises every row into (external_id, amount, date, raw), and
     reconciles it against our own records for the same window.
  3. The result splits every transaction into one of four buckets:
        - matched              (id + amount agree)
        - amount_mismatch      (same id, different amount)
        - only_in_statement    (in the bank file, not in our DB)
        - only_in_system       (in our DB, not in the bank file)
  4. The admin can SAVE the run. A saved run freezes the full result
     JSON and keeps the original uploaded statement file so it can be
     re-downloaded later from the audit history.

Only the SAVED runs persist. Ad-hoc "preview" runs are computed and
returned to the browser but never written to the DB.
"""
import uuid

from django.db import models


class BankAudit(models.Model):
    """A saved bank-reconciliation run.

    Holds the frozen result JSON + summary counters. The original
    uploaded statement lives on the related `BankAuditFile` row so it
    can be downloaded from the history later.
    """

    # Bank identifiers — mirror the statement formats we can parse.
    BANK_CASHAPP = "cashapp"
    BANK_AMEX = "amex"
    BANK_USBANK = "us_bank"
    BANK_GENERIC = "generic"
    BANK_CHOICES = [
        (BANK_CASHAPP, "Cash App"),
        (BANK_AMEX, "American Express"),
        (BANK_USBANK, "US Bank"),
        (BANK_GENERIC, "Generic / Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Human-friendly label the admin gives the saved audit, e.g.
    # "Cash App — May 2026".
    title = models.CharField(max_length=200)

    bank = models.CharField(
        max_length=20, choices=BANK_CHOICES, db_index=True,
        help_text="Which bank's statement format was reconciled.",
    )

    # The window the audit covered. Stored so the history shows it
    # without having to re-read the result JSON.
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    # Summary counters — denormalised from `result` for fast list views.
    total_statement = models.PositiveIntegerField(default=0)
    total_system = models.PositiveIntegerField(default=0)
    matched_count = models.PositiveIntegerField(default=0)
    amount_mismatch_count = models.PositiveIntegerField(default=0)
    only_in_statement_count = models.PositiveIntegerField(default=0)
    only_in_system_count = models.PositiveIntegerField(default=0)

    # The full frozen reconciliation result. Shape:
    #   {
    #     "summary": {...},
    #     "matched": [...],
    #     "amount_mismatch": [...],
    #     "only_in_statement": [...],
    #     "only_in_system": [...],
    #   }
    result = models.JSONField(default=dict, blank=True)

    notes = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bank_audits",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "bank_audits"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["bank", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.get_bank_display()})"


class BankAuditFile(models.Model):
    """The original statement file uploaded for a saved audit.

    Kept so the admin can re-download exactly what was reconciled.
    One file per saved audit (the admin uploads a single statement per
    run), but modelled as a FK so we can attach more later if needed.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audit = models.ForeignKey(
        BankAudit, on_delete=models.CASCADE, related_name="files",
    )
    file = models.FileField(upload_to="audits/statements/")
    original_name = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=120, blank=True, default="")
    size_bytes = models.PositiveIntegerField(default=0)

    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bank_audit_files"
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.original_name or str(self.file)
