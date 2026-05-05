from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Track which fields the customer changed on their most recent
    KYC resubmission, plus the timestamp. Surfaced in the admin
    review modal so reviewers can see at a glance what's new.

    Both fields default empty/null so existing rows migrate
    cleanly with no data backfill needed.
    """

    dependencies = [
        ('myapp', '0032_invoice_pdf_theme_default_dark'),
    ]

    operations = [
        migrations.AddField(
            model_name='customerprofile',
            name='kyc_last_resubmit_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='customerprofile',
            name='kyc_last_resubmit_changes',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "List of field names the customer changed in their "
                    "most recent resubmission (e.g. ['full_name', 'selfie'])."
                ),
            ),
        ),
    ]
