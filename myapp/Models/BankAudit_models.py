"""
Bank-reconciliation audit module.

This is SEPARATE from the action-log `AuditLog` in `Audit_models.py`.
Here we reconcile the company's recorded money movement (customer
`IncomingPayment` rows + company `InternalTransaction` rows) against the
statement files (CSV / Excel) the banks hand us.
"""
import uuid
from django.db import models


class BankAudit(models.Model):
    """A saved bank-reconciliation run."""
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
    title = models.CharField(max_length=200)
    bank = models.CharField(
        max_length=20, choices=BANK_CHOICES, db_index=True,
        help_text="Which bank's statement format was reconciled.",
    )
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    total_statement = models.PositiveIntegerField(default=0)
    total_system = models.PositiveIntegerField(default=0)
    matched_count = models.PositiveIntegerField(default=0)
    amount_mismatch_count = models.PositiveIntegerField(default=0)
    only_in_statement_count = models.PositiveIntegerField(default=0)
    only_in_system_count = models.PositiveIntegerField(default=0)

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
    """The original statement file uploaded for a saved audit."""
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


class BankStatementRecord(models.Model):
    """Normalized bank statement line item parsed from bank statement CSV/Excel exports."""
    DIRECTION_CHOICES = [
        ("C", "Credit (Inflow)"),
        ("D", "Debit (Outflow)"),
    ]

    account_number = models.CharField(max_length=64, db_index=True)
    channel_ref = models.CharField(max_length=128, blank=True, db_index=True)
    cr_dr = models.CharField(max_length=10, db_index=True)  # 'C' or 'D'
    tran_type = models.CharField(max_length=64, blank=True, db_index=True)

    amount = models.DecimalField(max_digits=16, decimal_places=2, db_index=True)
    currency = models.CharField(max_length=10, default="PKR")
    equiv_amount = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    equiv_currency = models.CharField(max_length=10, blank=True)

    running_balance = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    running_balance_currency = models.CharField(max_length=10, blank=True)

    tran_date = models.DateField(null=True, blank=True, db_index=True)
    post_date = models.DateField(null=True, blank=True, db_index=True)
    tran_ref = models.CharField(max_length=128, blank=True, db_index=True)

    tran_desc = models.TextField(blank=True)
    tran_desc2 = models.TextField(blank=True)
    tran_desc3 = models.TextField(blank=True)
    tran_desc4 = models.TextField(blank=True)

    raw_data = models.JSONField(default=dict, blank=True)
    unique_hash = models.CharField(max_length=64, unique=True, db_index=True)
    synced_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "bank_statement_records"
        ordering = ["-tran_date", "-id"]
        indexes = [
            models.Index(fields=["tran_date", "cr_dr"]),
            models.Index(fields=["account_number", "tran_date"]),
        ]

    def __str__(self):
        direction = "+" if self.cr_dr == "C" else "-"
        return f"{self.tran_date} | {direction}{self.currency} {self.amount} | {self.tran_ref or self.tran_desc[:30]}"


class BankSyncJob(models.Model):
    """Audit log for automated and manual bank statement sync runs."""
    STATUS_CHOICES = [
        ("running", "Running"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]

    source = models.CharField(max_length=50, default="cron")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="running", db_index=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    newly_inserted = models.IntegerField(default=0)
    skipped_duplicates = models.IntegerField(default=0)
    credits_inserted = models.IntegerField(default=0)
    debits_inserted = models.IntegerField(default=0)

    error_message = models.TextField(blank=True)

    class Meta:
        db_table = "bank_sync_jobs"
        ordering = ["-started_at"]

    def __str__(self):
        return f"BankSyncJob #{self.id} ({self.source}) - {self.status} at {self.started_at}"
