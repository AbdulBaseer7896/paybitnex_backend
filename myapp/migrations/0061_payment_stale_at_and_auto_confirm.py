"""
Re-introduce the customer-confirmation queue with a bounded lifetime.

Two pieces:

1. `IncomingPayment.stale_at` — the moment the payment was flagged stale
   and surfaced in the "Awaiting customer confirmation" queue. The old flow
   had no such field because nothing was ever anchored to it; the auto-
   confirm timer now is, so we need a stable point that isn't disturbed by
   `updated_at` moving for unrelated edits. Backfilled from `updated_at`
   for rows already flagged.

2. `auto_confirm_payment_minutes` SystemSetting — how long a payment sits
   in that queue before we approve it on the customer's behalf. Defaults to
   1440 (one day). Admin approval already implies the money was sent and
   verified, so an unresponsive customer shouldn't block the books forever.
"""
from django.db import migrations, models


def seed_setting(apps, schema_editor):
    SystemSetting = apps.get_model("myapp", "SystemSetting")
    SystemSetting.objects.get_or_create(
        key="auto_confirm_payment_minutes",
        defaults={
            "value": "1440",  # 1 day
            "description": (
                "Minutes a payment may sit in Awaiting Customer Confirmation "
                "before it is auto-approved on the customer's behalf. Timed "
                "from when the payment was flagged stale, not from when PKR "
                "was sent. Set to 0 to disable auto-approval."
            ),
        },
    )


def unseed_setting(apps, schema_editor):
    SystemSetting = apps.get_model("myapp", "SystemSetting")
    SystemSetting.objects.filter(key="auto_confirm_payment_minutes").delete()


def backfill_stale_at(apps, schema_editor):
    """Give already-flagged payments an anchor so they don't sit forever.

    Without this, every row flagged by the old task has stale_at=NULL and
    the auto-confirm task would either skip them permanently or (if we
    treated NULL as "now") restart their clock on every deploy.
    """
    IncomingPayment = apps.get_model("myapp", "IncomingPayment")
    IncomingPayment.objects.filter(
        is_stale=True, stale_at__isnull=True,
    ).update(stale_at=models.F("updated_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0060_extend_email_verification_grace_period"),
    ]

    operations = [
        migrations.AddField(
            model_name="incomingpayment",
            name="stale_at",
            field=models.DateTimeField(
                null=True, blank=True, db_index=True,
                help_text=(
                    "When this payment was flagged stale and entered the "
                    "Awaiting Customer Confirmation queue."
                ),
            ),
        ),
        migrations.AddField(
            model_name="incomingpayment",
            name="auto_confirmed",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True when the system approved this on the customer's "
                    "behalf after the auto-confirm window elapsed."
                ),
            ),
        ),
        migrations.RunPython(backfill_stale_at, migrations.RunPython.noop),
        migrations.RunPython(seed_setting, unseed_setting),
    ]
