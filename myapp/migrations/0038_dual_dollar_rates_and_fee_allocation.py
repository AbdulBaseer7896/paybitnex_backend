"""
Migration 0038: Dual dollar rates + fee allocation per transaction.

Changes:
1. IncomingPayment gains `real_exchange_rate` — the actual market rate
   at time of transaction. `exchange_rate` stays as the "tangent" (customer) rate.
   Company profit from rate spread = (real_rate - tangent_rate) * net_amount_foreign
   + same spread on partner payouts.
   
2. IncomingPayment gains `fee_allocation` (JSONField) — stores how the fee is
   split when total fee < total partner shares (the "under-fee" case).
   Format: {"company": <pct>, "partners": {"<partner_id>": <pct>}}
   
3. PartnerLedgerEntry gains `real_exchange_rate_snapshot` and 
   `company_rate_profit_pkr` to track rate-spread profit separately.
   
Zero data loss: all new fields are nullable/have defaults.
"""
from django.db import migrations, models
import django.core.validators
from decimal import Decimal


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0037_remove_external_tx_id_unique"),
    ]

    operations = [
        # Add real_exchange_rate to IncomingPayment
        migrations.AddField(
            model_name="incomingpayment",
            name="real_exchange_rate",
            field=models.DecimalField(
                max_digits=14, decimal_places=6, null=True, blank=True,
                help_text=(
                    "Actual market rate at time of transaction "
                    "(1 unit = X PKR). exchange_rate is the 'tangent' rate "
                    "given to customer. Spread = real - tangent."
                ),
            ),
        ),
        # Add fee_allocation JSON for under-fee handling
        migrations.AddField(
            model_name="incomingpayment",
            name="fee_allocation",
            field=models.JSONField(
                null=True, blank=True,
                help_text=(
                    "Fee allocation override when total fee < total partner shares. "
                    'Format: {"company": <pct>, "partners": {"<uuid>": <pct>}}'
                ),
            ),
        ),
        # Add real_exchange_rate_snapshot to PartnerLedgerEntry
        migrations.AddField(
            model_name="partnerledgerentry",
            name="real_exchange_rate_snapshot",
            field=models.DecimalField(
                max_digits=14, decimal_places=6, null=True, blank=True,
                help_text="Actual market rate used for company profit calculation.",
            ),
        ),
        # Add company_rate_profit_pkr to PartnerLedgerEntry 
        # (rate spread profit on this partner's portion)
        migrations.AddField(
            model_name="partnerledgerentry",
            name="rate_spread_profit_pkr",
            field=models.DecimalField(
                max_digits=18, decimal_places=2, null=True, blank=True,
                help_text=(
                    "Company's rate-spread profit from this partner's payout: "
                    "(real_rate - tangent_rate) * amount_foreign"
                ),
            ),
        ),
    ]
