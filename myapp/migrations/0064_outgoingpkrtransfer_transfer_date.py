from django.db import migrations, models


def backfill_transfer_date(apps, schema_editor):
    OutgoingPKRTransfer = apps.get_model("myapp", "OutgoingPKRTransfer")
    qs = OutgoingPKRTransfer.objects.filter(transfer_date__isnull=True).only("id", "sent_at")
    batch = []
    for t in qs.iterator(chunk_size=500):
        if t.sent_at:
            t.transfer_date = t.sent_at.date()
            batch.append(t)
        if len(batch) >= 500:
            OutgoingPKRTransfer.objects.bulk_update(batch, ["transfer_date"])
            batch = []
    if batch:
        OutgoingPKRTransfer.objects.bulk_update(batch, ["transfer_date"])


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0063_outgoingpkrtransferreceipt"),
    ]

    operations = [
        migrations.AddField(
            model_name="outgoingpkrtransfer",
            name="transfer_date",
            field=models.DateField(
                blank=True,
                db_index=True,
                help_text="Date the PKR transfer was executed at the bank",
                null=True,
            ),
        ),
        migrations.RunPython(backfill_transfer_date, reverse_code=migrations.RunPython.noop),
    ]
