from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0035_expense_distribution"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentmethod",
            name="is_default",
            field=models.BooleanField(
                default=False,
                help_text="Show this method to all customers by default (pre-selected).",
            ),
        ),
    ]
