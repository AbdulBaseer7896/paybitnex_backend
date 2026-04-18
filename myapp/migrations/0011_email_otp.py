import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0010_expense"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailOTP",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                         primary_key=True, serialize=False)),
                ("email", models.EmailField(db_index=True, max_length=254)),
                ("purpose", models.CharField(
                    choices=[("signup", "Signup"),
                             ("password_reset", "Password reset")],
                    db_index=True, max_length=20,
                )),
                ("code", models.CharField(max_length=6)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                "db_table": "email_otps",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["email", "purpose", "-created_at"],
                                 name="email_otps_email_purpose_idx"),
                ],
            },
        ),
    ]
