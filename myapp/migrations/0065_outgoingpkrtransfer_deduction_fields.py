from decimal import Decimal
from django.db import migrations, models


def backfill_deduction_fields(apps, schema_editor):
    OutgoingPKRTransfer = apps.get_model("myapp", "OutgoingPKRTransfer")
    qs = OutgoingPKRTransfer.objects.filter(original_amount_pkr__isnull=True).only("id", "amount_pkr")
    batch = []
    for t in qs.iterator(chunk_size=500):
        t.original_amount_pkr = t.amount_pkr
        t.deduction_pkr = Decimal("0.00")
        batch.append(t)
        if len(batch) >= 500:
            OutgoingPKRTransfer.objects.bulk_update(batch, ["original_amount_pkr", "deduction_pkr"])
            batch = []
    if batch:
        OutgoingPKRTransfer.objects.bulk_update(batch, ["original_amount_pkr", "deduction_pkr"])


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0064_outgoingpkrtransfer_transfer_date"),
    ]

    operations = [
        migrations.AddField(
            model_name="outgoingpkrtransfer",
            name="original_amount_pkr",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Original PKR equivalent amount before deductions",
                max_digits=18,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="outgoingpkrtransfer",
            name="deduction_pkr",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Deductions applied in PKR (e.g. customer owes money)",
                max_digits=18,
            ),
        ),
        migrations.RunPython(backfill_deduction_fields, reverse_code=migrations.RunPython.noop),
    ]
