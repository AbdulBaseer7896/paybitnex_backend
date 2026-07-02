"""
Migration 0047: Card-transaction dollar rate + PKR profit.

Adds two additive, nullable fields to InternalTransaction (zero data loss):

  - card_dollar_rate (decimal, nullable)  PKR per 1 unit of `currency`
                                          for a credit-card transaction.
  - card_profit_pkr  (decimal, nullable)  amount × card_dollar_rate,
                                          booked as company profit and
                                          surfaced in the overview and
                                          closing reports.

Only meaningful when source_type = 'credit_card'; the serializer clears
both for any other source so a stray value can't pollute profit reporting.
Existing rows keep working unchanged (both fields default to NULL).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0046_user_payments_pin_hash"),
    ]

    operations = [
        migrations.AddField(
            model_name="internaltransaction",
            name="card_dollar_rate",
            field=models.DecimalField(
                max_digits=14, decimal_places=6, null=True, blank=True,
                help_text="PKR value per 1 unit of `currency` for a card "
                          "transaction (1 USD = N PKR). Only used when "
                          "source_type = 'credit_card'.",
            ),
        ),
        migrations.AddField(
            model_name="internaltransaction",
            name="card_profit_pkr",
            field=models.DecimalField(
                max_digits=20, decimal_places=2, null=True, blank=True,
                help_text="Company profit in PKR from this card "
                          "transaction: amount × card_dollar_rate. Counted "
                          "as company profit in the overview and closing "
                          "reports.",
            ),
        ),
    ]
