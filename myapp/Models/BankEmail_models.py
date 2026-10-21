"""Stores raw bank-alert emails + the extracted fields + match status."""
import uuid
from django.db import models


class BankAlertStatus(models.TextChoices):
    UNMATCHED = "unmatched", "Unmatched"
    AUTO_MATCHED = "auto_matched", "Auto Matched"
    CONFLICT = "conflict", "Conflict — Needs Review"
    MANUAL_MATCHED = "manual_matched", "Manually Matched"
    IGNORED = "ignored", "Ignored"


class BankAlertEmail(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Raw email metadata — message_id is unique so the poller can be
    # idempotent across retries and process restarts.
    message_id = models.CharField(max_length=512, unique=True, db_index=True)
    sender_email = models.CharField(max_length=255, blank=True, db_index=True)
    subject = models.CharField(max_length=500, blank=True)
    received_at = models.DateTimeField(db_index=True)
    raw_body_text = models.TextField(blank=True)
    raw_body_html = models.TextField(blank=True)

    # Detected + extracted fields from the parser
    bank_type = models.CharField(max_length=50, blank=True, db_index=True)
    extracted_amount = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
    )
    extracted_currency = models.CharField(max_length=10, blank=True, default="USD")
    extracted_sender_name = models.CharField(max_length=200, blank=True)
    extracted_reference = models.CharField(max_length=200, blank=True)
    extraction_notes = models.TextField(blank=True)
    parser_error = models.TextField(blank=True)

    # Matching state
    status = models.CharField(
        max_length=20, choices=BankAlertStatus.choices,
        default=BankAlertStatus.UNMATCHED, db_index=True,
    )
    matched_payment = models.ForeignKey(
        "myapp.IncomingPayment", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="matched_bank_emails",
    )
    conflict_payment_ids = models.JSONField(default=list, blank=True)
    matched_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="matched_bank_emails",
    )
    matched_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "bank_alert_emails"
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["status", "received_at"]),
            models.Index(fields=["bank_type", "status"]),
        ]

    def __str__(self):
        return (
            f"{self.bank_type or 'unknown'} · "
            f"{self.extracted_amount or '?'} {self.extracted_currency} · "
            f"{self.status}"
        )