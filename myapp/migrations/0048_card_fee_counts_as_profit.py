"""
Migration 0048: Card-transaction bank fee counts as company PROFIT.

Business rule change:
  On CREDIT-CARD internal transactions the bank fee is NOT a cost —
  it belongs to the company. It is still displayed as the bank fee on
  the transaction, but:

    card_profit_pkr = (amount + fee_amount) × card_dollar_rate

  and the fee is never pushed into the Expenses table (previously it
  auto-created a BANKING expense, which wrongly reduced net profit on
  the overview / closing reports).

This migration:
  1. Alters the two help_texts (no schema change, keeps makemigrations clean).
  2. Data-fix pass over existing credit-card rows:
       a. deletes any auto-created fee Expense still linked to a
          credit-card transaction (and unlinks it), so historical card
          fees stop counting as costs;
       b. recomputes card_profit_pkr to include the fee for every row
          that has a card_dollar_rate.

Reverse: help_texts revert; the data fix is intentionally irreversible
(deleted expense rows can't be resurrected), so reverse is a no-op for
the data part.
"""
from decimal import Decimal

from django.db import migrations, models


def _fix_card_rows(apps, schema_editor):
    InternalTransaction = apps.get_model("myapp", "InternalTransaction")
    Expense = apps.get_model("myapp", "Expense")

    qs = InternalTransaction.objects.filter(source_type="credit_card")
    for tx in qs.iterator():
        changed_fields = []

        # (a) Remove the wrongly-created fee expense for card txns.
        if tx.fee_expense_id:
            exp_id = tx.fee_expense_id
            tx.fee_expense = None
            changed_fields.append("fee_expense")
            try:
                Expense.objects.filter(pk=exp_id).delete()
            except Exception:
                pass

        # (b) Recompute profit to include the bank fee.
        if tx.card_dollar_rate is not None:
            amount = Decimal(str(tx.amount or "0"))
            fee = Decimal(str(tx.fee_amount or "0"))
            if fee < 0:
                fee = Decimal("0")
            new_profit = ((amount + fee) * Decimal(str(tx.card_dollar_rate))
                          ).quantize(Decimal("0.01"))
            if tx.card_profit_pkr != new_profit:
                tx.card_profit_pkr = new_profit
                changed_fields.append("card_profit_pkr")

        if changed_fields:
            tx.save(update_fields=changed_fields)


def _noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0047_internal_card_dollar_rate_profit"),
    ]

    operations = [
        migrations.AlterField(
            model_name="internaltransaction",
            name="fee_amount",
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=18,
                help_text=(
                    "Bank / wire / processing fee charged on this "
                    "transfer, in `currency`. For bank transfers it is "
                    "auto-pushed into Expenses in the BANKING category. "
                    "For CREDIT-CARD transactions the fee belongs to the "
                    "company: it is shown as the bank fee but booked as "
                    "PROFIT (folded into card_profit_pkr), never as an "
                    "expense."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="internaltransaction",
            name="card_profit_pkr",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=20, null=True,
                help_text=(
                    "Company profit in PKR from this card transaction: "
                    "(amount + fee_amount) × card_dollar_rate. The bank "
                    "fee on card spend belongs to the company, so it is "
                    "included here. Counted as company profit in the "
                    "overview and closing reports."
                ),
            ),
        ),
        migrations.RunPython(_fix_card_rows, _noop),
    ]
