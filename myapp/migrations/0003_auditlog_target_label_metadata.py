"""Adds target_label and metadata to AuditLog for richer activity tracking."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0002_selfie_and_rating"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="target_label",
            field=models.CharField(
                max_length=200, blank=True, default="",
                help_text="Human-readable short label of the target object at log time.",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="auditlog",
            name="metadata",
            field=models.JSONField(
                null=True, blank=True,
                help_text="Request path, extra context, etc.",
            ),
        ),
    ]
