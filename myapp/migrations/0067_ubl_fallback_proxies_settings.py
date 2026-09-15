"""Seed UBL scraper fallback proxy settings in SystemSetting."""
from django.db import migrations


def seed_proxies(apps, schema_editor):
    SystemSetting = apps.get_model("myapp", "SystemSetting")
    defaults = [
        (
            "ubl_fallback_proxy_1",
            "",
            "UBL scraper fallback proxy #1 (host:port:user:pass). First priority fallback if primary proxy fails or reaches quota limit.",
        ),
        (
            "ubl_fallback_proxy_2",
            "",
            "UBL scraper fallback proxy #2 (host:port:user:pass). Second priority fallback if proxy #1 fails.",
        ),
        (
            "ubl_fallback_proxy_3",
            "",
            "UBL scraper fallback proxy #3 (host:port:user:pass). Third priority fallback if proxy #2 fails.",
        ),
    ]
    for key, val, desc in defaults:
        SystemSetting.objects.get_or_create(
            key=key,
            defaults={
                "value": val,
                "description": desc,
            },
        )


def unseed_proxies(apps, schema_editor):
    SystemSetting = apps.get_model("myapp", "SystemSetting")
    SystemSetting.objects.filter(
        key__in=["ubl_fallback_proxy_1", "ubl_fallback_proxy_2", "ubl_fallback_proxy_3"]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0066_outgoingpkrtransfer_bank_verification"),
    ]

    operations = [
        migrations.RunPython(seed_proxies, unseed_proxies),
    ]
