import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0009_unique_ids"),
    ]

    operations = [
        migrations.CreateModel(
            name="Expense",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                         primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=200)),
                ("category", models.CharField(
                    choices=[
                        ("subscription", "Subscription / SaaS"),
                        ("banking", "Bank fees / charges"),
                        ("partner", "Partner payout"),
                        ("office", "Office / operations"),
                        ("payroll", "Payroll / salaries"),
                        ("tax", "Tax / regulatory"),
                        ("marketing", "Marketing / advertising"),
                        ("other", "Other"),
                    ],
                    db_index=True, default="other", max_length=30,
                )),
                ("vendor", models.CharField(
                    blank=True, max_length=150,
                    help_text="Who was paid — e.g. 'Stripe', 'HBL', 'AWS'.",
                )),
                ("amount", models.DecimalField(decimal_places=2, max_digits=18)),
                ("purpose", models.TextField(
                    blank=True,
                    help_text="Free-form description of what this expense was for.",
                )),
                ("spent_on", models.DateField(
                    db_index=True,
                    help_text="Date the expense was incurred (not the date it was entered).",
                )),
                ("document", models.FileField(
                    blank=True, null=True, upload_to="expenses/docs/",
                    help_text="Receipt / invoice / screenshot — accepts images or PDFs.",
                )),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(
                    on_delete=models.deletion.PROTECT,
                    related_name="created_expenses",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("currency", models.ForeignKey(
                    on_delete=models.deletion.PROTECT,
                    related_name="expenses",
                    to="myapp.currency", to_field="code",
                )),
            ],
            options={
                "ordering": ["-spent_on", "-created_at"],
                "indexes": [
                    models.Index(fields=["spent_on", "category"],
                                 name="myapp_expen_spent_o_c52b7f_idx"),
                    models.Index(fields=["currency", "spent_on"],
                                 name="myapp_expen_currenc_fe823a_idx"),
                ],
            },
        ),
    ]
