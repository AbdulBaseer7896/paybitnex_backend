"""Create InvoicePaymentMethod through-table for multi-method invoices."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0019_default_invoice_prefix"),
    ]

    operations = [
        migrations.CreateModel(
            name="InvoicePaymentMethod",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False)),
                ("snapshot", models.JSONField(blank=True, default=dict)),
                ("position", models.PositiveSmallIntegerField(default=0)),
                ("invoice", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="invoice_payment_methods",
                    to="myapp.invoice")),
                ("payment_method", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="myapp.paymentmethod")),
            ],
            options={
                "db_table": "invoice_payment_methods",
                "ordering": ["position", "id"],
                "unique_together": {("invoice", "payment_method")},
            },
        ),
    ]
