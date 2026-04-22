"""Seed the admin-wide default invoice number prefix setting."""
from django.db import migrations


def seed(apps, schema_editor):
    SystemSetting = apps.get_model("myapp", "SystemSetting")
    SystemSetting.objects.get_or_create(
        key="invoice_number_prefix",
        defaults={
            "value": "BIT",
            "description": (
                "Default prefix used when generating invoice numbers. "
                "Individual customers can override this on a per-company "
                "basis in Settings → My Companies; when they leave the "
                "field blank, this admin default is used. Falls back to "
                "'BIT' if neither is set."
            ),
        },
    )


def unseed(apps, schema_editor):
    SystemSetting = apps.get_model("myapp", "SystemSetting")
    SystemSetting.objects.filter(key="invoice_number_prefix").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0018_invoices"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
