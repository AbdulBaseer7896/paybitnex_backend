from datetime import timedelta

from django.db import migrations
from django.utils import timezone


def extend_grace_period(apps, schema_editor):
    User = apps.get_model("myapp", "User")
    # Only legacy users carry a deadline. New/admin-created unverified users
    # deliberately keep NULL and must verify before their first login.
    User.objects.exclude(role="admin").filter(
        email_verified=False,
        verification_deadline__isnull=False,
    ).update(
        is_active=True,
        verification_deadline=timezone.now() + timedelta(days=14),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0059_merge_20260813_0000"),
    ]

    operations = [
        migrations.RunPython(extend_grace_period, migrations.RunPython.noop),
    ]
