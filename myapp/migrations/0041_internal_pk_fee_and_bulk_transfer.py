"""
Migration 0041: Internal-transaction PK-bank fee + conversion rate, and
support for one PKR transfer covering many customer payments.

Adds (all additive, zero data loss):

  InternalTransaction:
    - pk_fee_percent    (decimal, default 0)   PK bank fee as a % of gross
    - pk_fee_amount     (decimal, default 0)   resolved PK fee in `currency`
    - pk_conversion_rate(decimal, nullable)    1 USD = N PKR the bank gave
    - pk_amount_pkr     (decimal, nullable)    net PKR landed
    - pk_fee_expense    (FK Expense, nullable) auto-expense for the PK fee

  OutgoingPKRTransfer:
    - incoming_payment  made nullable (was OneToOne, required) so a bulk
      transfer can populate the new M2M instead.
    - payments          (M2M IncomingPayment) one transfer → many payments

Existing single-payment transfers keep working unchanged: they still use
`incoming_payment`, and nothing about their rows is rewritten.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0040_auto_assign_default_payment_methods"),
    ]

    operations = [
        # ── InternalTransaction: PK-side fee + conversion rate ──────────
        migrations.AddField(
            model_name="internaltransaction",
            name="pk_fee_percent",
            field=models.DecimalField(
                max_digits=6, decimal_places=4, default=0,
                help_text="Pakistani bank fee as a percent of the gross "
                          "amount (e.g. 0.25 = 0.25%). Only used for "
                          "USA→PK transfers.",
            ),
        ),
        migrations.AddField(
            model_name="internaltransaction",
            name="pk_fee_amount",
            field=models.DecimalField(
                max_digits=18, decimal_places=2, default=0,
                help_text="Resolved PK bank fee in `currency` (amount × "
                          "pk_fee_percent / 100). Auto-pushed into Expenses "
                          "in the BANKING category, like the USA-side fee.",
            ),
        ),
        migrations.AddField(
            model_name="internaltransaction",
            name="pk_conversion_rate",
            field=models.DecimalField(
                max_digits=14, decimal_places=6, null=True, blank=True,
                help_text="PKR the receiving bank paid per 1 unit of "
                          "`currency` (1 USD = N PKR). Only used for "
                          "USA→PK transfers.",
            ),
        ),
        migrations.AddField(
            model_name="internaltransaction",
            name="pk_amount_pkr",
            field=models.DecimalField(
                max_digits=20, decimal_places=2, null=True, blank=True,
                help_text="Net PKR that landed: (amount − pk_fee_amount) × "
                          "pk_conversion_rate. Stored for reporting.",
            ),
        ),
        migrations.AddField(
            model_name="internaltransaction",
            name="pk_fee_expense",
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="internal_transaction_pk_fees",
                to="myapp.expense",
            ),
        ),
        # ── OutgoingPKRTransfer: one transfer → many payments ───────────
        migrations.AlterField(
            model_name="outgoingpkrtransfer",
            name="incoming_payment",
            field=models.OneToOneField(
                null=True, blank=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="outgoing_transfer",
                to="myapp.incomingpayment",
            ),
        ),
        migrations.AddField(
            model_name="outgoingpkrtransfer",
            name="payments",
            field=models.ManyToManyField(
                blank=True,
                related_name="covering_transfers",
                to="myapp.incomingpayment",
            ),
        ),
    ]
