"""
Remove the DB-level unique constraint on external_transaction_id.

Uniqueness is now enforced at the serializer level with rejected-payment
awareness: rejected payments release their external ID so the customer
can resubmit with the same reference. A plain DB UNIQUE constraint
cannot express this conditional logic, so we drop it and let the
serializer handle it.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0036_paymentmethod_is_default"),
    ]

    operations = [
        migrations.AlterField(
            model_name="incomingpayment",
            name="external_transaction_id",
            field=models.CharField(
                max_length=100,
                db_index=True,
                help_text=(
                    "Unique ID from sender's bank / platform. "
                    "Rejected payments release their ID for reuse — "
                    "uniqueness is enforced at the serializer level (not DB)."
                ),
            ),
        ),
    ]
