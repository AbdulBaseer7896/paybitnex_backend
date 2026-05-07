"""
Migration: add ExpenseDistribution model and InternalTransaction.fee_distribution_* fields.

ExpenseDistribution tracks how an expense is split between partners and the company.
Each row represents one slice (partner or company). Slices sum to the expense.amount.

InternalTransaction gets two fields:
  fee_dist_type   — 'company' | 'partner' | 'custom'
  fee_dist_partner— FK to Partner (used when fee_dist_type = 'partner')
"""
import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0034_rename_dispatches_customer_stat_idx_dispatches_custome_37c4aa_idx_and_more"),
    ]

    operations = [
        # ── ExpenseDistribution ──────────────────────────────────────
        migrations.CreateModel(
            name="ExpenseDistribution",
            fields=[
                ("id", models.UUIDField(
                    primary_key=True, default=uuid.uuid4, editable=False,
                    serialize=False,
                )),
                ("expense", models.ForeignKey(
                    to="myapp.Expense",
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="distributions",
                )),
                # NULL partner_id = company slice
                ("partner", models.ForeignKey(
                    to="myapp.Partner",
                    on_delete=django.db.models.deletion.PROTECT,
                    null=True, blank=True,
                    related_name="expense_distributions",
                )),
                ("amount", models.DecimalField(
                    max_digits=18, decimal_places=2,
                    help_text="Amount in the expense's currency assigned to this slice.",
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(
                    to=settings.AUTH_USER_MODEL,
                    on_delete=django.db.models.deletion.SET_NULL,
                    null=True, blank=True,
                    related_name="expense_distribution_updates",
                )),
            ],
            options={
                "db_table": "expense_distributions",
                "ordering": ["partner__name"],
            },
        ),
        migrations.AddIndex(
            model_name="ExpenseDistribution",
            index=models.Index(
                fields=["expense"], name="exp_dist_expense_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="ExpenseDistribution",
            index=models.Index(
                fields=["partner"], name="exp_dist_partner_idx",
            ),
        ),

        # ── InternalTransaction fee distribution fields ──────────────
        migrations.AddField(
            model_name="InternalTransaction",
            name="fee_dist_type",
            field=models.CharField(
                max_length=10,
                choices=[
                    ("company", "Company"),
                    ("partner", "Partner"),
                    ("custom", "Custom (split)"),
                ],
                default="company",
                help_text="Who absorbs the transfer fee — company, a single partner, or custom split.",
            ),
        ),
        migrations.AddField(
            model_name="InternalTransaction",
            name="fee_dist_partner",
            field=models.ForeignKey(
                to="myapp.Partner",
                on_delete=django.db.models.deletion.SET_NULL,
                null=True, blank=True,
                related_name="internal_tx_fees",
                help_text="Partner who absorbs the full fee (used when fee_dist_type=partner).",
            ),
        ),
    ]
