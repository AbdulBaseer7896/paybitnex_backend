"""
Adds the Verification stage (before Apply-Rate-Fee) to IncomingPayment.

Introduces:
    - verified_note         : accountant's short note
    - verified_document     : optional proof pic uploaded by accountant
    - verified_by           : who verified
    - verified_at           : when verified  (pre-existing, kept)
Drops nothing — `verified_at` already existed on 0001.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0003_auditlog_target_label_metadata"),
    ]

    operations = [
        migrations.AddField(
            model_name="incomingpayment",
            name="verified_note",
            field=models.TextField(
                blank=True,
                help_text="Short note the accountant writes when verifying documents.",
            ),
        ),
        migrations.AddField(
            model_name="incomingpayment",
            name="verified_document",
            field=models.FileField(
                blank=True, null=True, upload_to="proofs/verification/",
                help_text="Optional proof the accountant uploads while verifying.",
            ),
        ),
        migrations.AddField(
            model_name="incomingpayment",
            name="verified_by",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="verified_payments",
                to="myapp.user",
            ),
        ),
    ]
