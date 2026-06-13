"""
Migration 0043: Bank-reconciliation audit module.

Additive only — two new tables, zero changes to existing models:

  BankAudit       a saved reconciliation run (frozen result JSON +
                  summary counters + the period it covered).
  BankAuditFile   the original uploaded statement file for a saved
                  audit, so it can be re-downloaded from the history.

Nothing here touches customer/internal transaction tables — the audit
reads from them at run time but stores only its own result.
"""
import uuid

from django.conf import settings
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0041_internal_pk_fee_and_bulk_transfer"),
    ]

    operations = [
        migrations.CreateModel(
            name="BankAudit",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=200)),
                ("bank", models.CharField(
                    choices=[
                        ("cashapp", "Cash App"),
                        ("amex", "American Express"),
                        ("us_bank", "US Bank"),
                        ("generic", "Generic / Other"),
                    ],
                    db_index=True, max_length=20,
                )),
                ("period_start", models.DateField(blank=True, null=True)),
                ("period_end", models.DateField(blank=True, null=True)),
                ("total_statement", models.PositiveIntegerField(default=0)),
                ("total_system", models.PositiveIntegerField(default=0)),
                ("matched_count", models.PositiveIntegerField(default=0)),
                ("amount_mismatch_count", models.PositiveIntegerField(default=0)),
                ("only_in_statement_count", models.PositiveIntegerField(default=0)),
                ("only_in_system_count", models.PositiveIntegerField(default=0)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="bank_audits",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "db_table": "bank_audits",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="bankaudit",
            index=models.Index(
                fields=["bank", "-created_at"],
                name="bank_audits_bank_idx",
            ),
        ),
        migrations.CreateModel(
            name="BankAuditFile",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("file", models.FileField(upload_to="audits/statements/")),
                ("original_name", models.CharField(blank=True, default="", max_length=255)),
                ("content_type", models.CharField(blank=True, default="", max_length=120)),
                ("size_bytes", models.PositiveIntegerField(default=0)),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                ("audit", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="files",
                    to="myapp.bankaudit",
                )),
            ],
            options={
                "db_table": "bank_audit_files",
                "ordering": ["-uploaded_at"],
            },
        ),
    ]
