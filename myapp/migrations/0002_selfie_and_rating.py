"""Adds selfie image and rating/scoring fields to CustomerProfile."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="customerprofile",
            name="selfie",
            field=models.ImageField(null=True, blank=True, upload_to="cnic/selfie/"),
        ),
        migrations.AddField(
            model_name="customerprofile",
            name="rating_tier",
            field=models.CharField(
                max_length=20,
                default="new",
                db_index=True,
                choices=[
                    ("new", "New"),
                    ("bronze", "Bronze"),
                    ("silver", "Silver"),
                    ("gold", "Gold"),
                    ("platinum", "Platinum"),
                ],
            ),
        ),
        migrations.AddField(
            model_name="customerprofile",
            name="rating_score",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="customerprofile",
            name="completed_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="customerprofile",
            name="rejected_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="customerprofile",
            name="total_volume_pkr",
            field=models.DecimalField(
                max_digits=16, decimal_places=2, default=0,
            ),
        ),
    ]
