"""
Seed the new `stale_payment_minutes` SystemSetting and a
`allow_customer_confirm_note` flag.

Non-destructive — only creates rows that don't already exist. If the admin
later changes the values via the UI, this migration never overwrites them
because it uses get_or_create with `defaults=`.
"""
from django.db import migrations


def seed_settings(apps, schema_editor):
    SystemSetting = apps.get_model("myapp", "SystemSetting")

    # Only set stale_payment_minutes if it doesn't already exist. Don't
    # clobber stale_payment_days either — the task falls through from
    # minutes → days so both can coexist, with minutes winning.
    SystemSetting.objects.get_or_create(
        key="stale_payment_minutes",
        defaults={
            "value": "4320",  # 4320 min = 3 days, matches legacy default
            "description": (
                "Minutes to wait after PKR is sent before a payment is "
                "flagged stale and moves to Awaiting Customer Confirmation. "
                "If unset, falls back to stale_payment_days × 1440."
            ),
        },
    )

    # Allow customers to attach a short note when they confirm receipt.
    # Stored as a string because SystemSetting.value is CharField/TextField.
    SystemSetting.objects.get_or_create(
        key="allow_customer_confirm_note",
        defaults={
            "value": "true",
            "description": (
                "When 'true', the customer sees a note textarea when clicking "
                "'I received my PKR'. When 'false', they confirm with a single "
                "click and no prompt."
            ),
        },
    )


def unseed_settings(apps, schema_editor):
    SystemSetting = apps.get_model("myapp", "SystemSetting")
    SystemSetting.objects.filter(
        key__in=["stale_payment_minutes", "allow_customer_confirm_note"],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        # Depends on whichever migration was most recently applied on the
        # user's DB — after 0013 the user's local makemigrations produced
        # 0014_rename_incoming_pa_status_is_stal_idx... so we chain off it.
        ("myapp", "0014_rename_incoming_pa_status_is_stal_idx_incoming_pa_status_90e4ef_idx_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_settings, unseed_settings),
    ]
