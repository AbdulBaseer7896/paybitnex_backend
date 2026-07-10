"""
Migration 0049: PaymentMethod.deposit_account — method → USA bank mapping.

Powers the Bank Statement page. Customer payments are recorded against a
payment METHOD (Zelle, Cash App, ACH/Wire, Payoneer) but a bank statement
needs to attribute each receipt to an ACCOUNT. In real US banking every
method resolves to exactly one account:

  • Zelle is enrolled to a single bank account (e.g. Chase Business).
  • Cash App's balance IS an account of its own.
  • ACH / Wire instructions carry a specific account + routing number.
  • Payoneer settles into a designated receiving account.

This nullable FK lets the admin declare that mapping once in
Settings → Payment methods. The statement API then rolls every
IncomingPayment up to `payment_method.deposit_account`; methods without a
mapping appear under an "Unassigned" bucket so nothing silently vanishes.

Nullable + SET_NULL so deleting a bank never breaks a method, and existing
rows need no data backfill.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0048_card_fee_counts_as_profit"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentmethod",
            name="deposit_account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="payment_methods",
                to="myapp.usabankaccount",
                help_text=(
                    "The company USA bank account where money received via "
                    "this method is deposited (e.g. Zelle → Chase Business)."
                ),
            ),
        ),
    ]
