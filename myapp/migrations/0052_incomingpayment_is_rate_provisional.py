"""
Migration 0052: IncomingPayment.is_rate_provisional + default-rate backfill.

WHY
---
The weekly report was blank until every transaction had been verified and
given a dollar rate by hand. Transactions now receive a PROVISIONAL rate
at creation time so reports have a PKR figure immediately; this column
records whether the rate currently on a row is that placeholder (True) or
a real rate entered by an accountant (False).

TWO PARTS
---------
1. Schema: add the boolean, defaulting to False. Existing rows are
   therefore treated as non-provisional, which is correct — any rate they
   already carry was typed in by a human.

2. Data backfill (`_backfill_default_rates`): historical rows that have NO
   rate at all are stamped with the current default so they stop being
   invisible in the new report. Those rows — and only those — are marked
   provisional.

   The backfill is deliberately conservative:
     • Rows that already have an `exchange_rate` are never touched.
     • REJECTED rows are skipped (they are excluded from the report).
     • Only `exchange_rate` and the reference-only `gross_pkr` are set.
       `net_pkr`, `fee_amount_foreign` and `net_amount_foreign` are left
       NULL because they depend on `fee_percentage`, a commercial decision.
       This guarantees the backfill cannot alter any profit figure,
       partner ledger entry, or customer-facing payout amount.
     • If no default rate can be resolved (no SystemSetting override and
       no ExchangeRate row), the migration does nothing at all rather
       than inventing a number.

REVERSIBILITY
-------------
`_unbackfill` clears the rate on exactly the rows this migration stamped
(identified by is_rate_provisional=True), so a rollback leaves the data as
it was found. The column is then dropped by the auto-generated reverse of
AddField.
"""
from decimal import Decimal, InvalidOperation

from django.db import migrations, models


BATCH = 500


def _resolve_default_rate(apps, currency_code="USD"):
    """Mirror of Utils/default_rate.get_default_rate using historical models.

    Migrations must not import application code directly (it may have moved
    on), so the resolution order is duplicated here against `apps.get_model`.
    """
    SystemSetting = apps.get_model("myapp", "SystemSetting")
    ExchangeRate = apps.get_model("myapp", "ExchangeRate")

    def clean(v):
        if v is None:
            return None
        try:
            d = Decimal(str(v).strip())
        except (InvalidOperation, ValueError, AttributeError):
            return None
        return d if d > 0 else None

    row = SystemSetting.objects.filter(pk="default_dollar_rate").first()
    if row is not None:
        val = clean(row.value)
        if val is not None:
            return val

    er = ExchangeRate.objects.filter(currency_id=currency_code).first()
    if er is not None:
        return clean(er.rate_to_pkr)

    return None


def _backfill_default_rates(apps, schema_editor):
    IncomingPayment = apps.get_model("myapp", "IncomingPayment")

    # Cache one rate per currency so we don't re-query per row.
    rate_cache = {}

    qs = (
        IncomingPayment.objects
        .filter(exchange_rate__isnull=True)
        .exclude(status="rejected")
        .only("id", "amount", "currency_id", "exchange_rate", "gross_pkr")
    )

    pending = []
    for payment in qs.iterator(chunk_size=BATCH):
        ccy = payment.currency_id or "USD"
        if ccy not in rate_cache:
            rate_cache[ccy] = _resolve_default_rate(apps, ccy)
        rate = rate_cache[ccy]
        if rate is None:
            continue

        payment.exchange_rate = rate
        payment.is_rate_provisional = True
        try:
            payment.gross_pkr = (
                Decimal(str(payment.amount)) * rate
            ).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            payment.gross_pkr = None

        pending.append(payment)
        if len(pending) >= BATCH:
            IncomingPayment.objects.bulk_update(
                pending,
                ["exchange_rate", "gross_pkr", "is_rate_provisional"],
            )
            pending = []

    if pending:
        IncomingPayment.objects.bulk_update(
            pending,
            ["exchange_rate", "gross_pkr", "is_rate_provisional"],
        )


def _unbackfill(apps, schema_editor):
    """Undo exactly what the forward backfill stamped."""
    IncomingPayment = apps.get_model("myapp", "IncomingPayment")
    IncomingPayment.objects.filter(is_rate_provisional=True).update(
        exchange_rate=None, gross_pkr=None, is_rate_provisional=False,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0051_paymentmethod_method_type_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="incomingpayment",
            name="is_rate_provisional",
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text=(
                    "True when exchange_rate is an auto-assigned placeholder "
                    "awaiting the accountant's real rate."
                ),
            ),
        ),
        migrations.RunPython(_backfill_default_rates, _unbackfill),
    ]
