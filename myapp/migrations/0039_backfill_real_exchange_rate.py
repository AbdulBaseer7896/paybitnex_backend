"""
Migration 0039: Backfill real_exchange_rate for existing transactions.

For all existing IncomingPayment rows where real_exchange_rate is NULL
but exchange_rate is set, copy exchange_rate → real_exchange_rate.

This means existing transactions show zero rate-spread profit (correct —
we didn't know the real rate at the time), and the "Edit Actual Dollar Rate"
button will let admin update them going forward.

Zero data loss: only fills NULL values, never overwrites existing data.
"""
from django.db import migrations


def backfill_real_rate(apps, schema_editor):
    IncomingPayment = apps.get_model("myapp", "IncomingPayment")
    # Bulk update: set real_exchange_rate = exchange_rate where real is NULL
    # and exchange_rate is not NULL
    IncomingPayment.objects.filter(
        real_exchange_rate__isnull=True,
        exchange_rate__isnull=False,
    ).update(real_exchange_rate=models.F("exchange_rate"))


def reverse_backfill(apps, schema_editor):
    # Reversing is a no-op — we don't clear real_exchange_rate on reverse
    # because we can't tell which ones were backfilled vs explicitly set.
    pass


from django.db import models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0038_dual_dollar_rates_and_fee_allocation"),
    ]

    operations = [
        migrations.RunPython(backfill_real_rate, reverse_backfill),
    ]
