from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0006_alter_auditlog_action_alter_auditlog_target_label"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customerprofile",
            name="kyc_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending Review"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                    ("objections", "Objections Raised"),
                    ("resubmitted", "Resubmitted for Review"),
                ],
                db_index=True,
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="customerprofile",
            name="kyc_objections",
            field=models.JSONField(
                blank=True, default=list,
                help_text="List of active objection entries: [{field, message, raised_at, raised_by}]",
            ),
        ),
        migrations.AddField(
            model_name="customerprofile",
            name="kyc_objection_round",
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text="Number of objection rounds this profile has gone through.",
            ),
        ),
        migrations.AddField(
            model_name="customerprofile",
            name="kyc_approved_at",
            field=models.DateTimeField(
                blank=True, null=True,
                help_text="Set when KYC moves to APPROVED. Profile becomes locked after this.",
            ),
        ),
    ]
