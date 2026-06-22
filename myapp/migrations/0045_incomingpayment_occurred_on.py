"""Add `occurred_on` business-date field to IncomingPayment.

`created_at` records when a payment row was entered into the system.
`occurred_on` records the date the payment actually happened — it defaults
to the entry date but staff (and the customer/staff batch entry grids) can
override it so a payment recorded a day or two late still carries its true
date. This date is what the customer portal shows as the official date.

The data migration backfills existing rows so `occurred_on` equals the date
portion of their `created_at`.
"""
from django.db import migrations, models


def backfill_occurred_on(apps, schema_editor):
    IncomingPayment = apps.get_model("myapp", "IncomingPayment")
    # Backfill in chunks to avoid loading everything into memory at once.
    qs = IncomingPayment.objects.filter(occurred_on__isnull=True).only(
        "id", "created_at",
    )
    batch = []
    for p in qs.iterator(chunk_size=500):
        if p.created_at:
            p.occurred_on = p.created_at.date()
            batch.append(p)
        if len(batch) >= 500:
            IncomingPayment.objects.bulk_update(batch, ["occurred_on"])
            batch = []
    if batch:
        IncomingPayment.objects.bulk_update(batch, ["occurred_on"])


def noop_reverse(apps, schema_editor):
    # Nothing to undo for the backfill — the column drop handles it.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0044_rename_bank_audits_bank_idx_bank_audits_bank_97efea_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="incomingpayment",
            name="occurred_on",
            field=models.DateField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(backfill_occurred_on, noop_reverse),
    ]
