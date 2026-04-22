"""Create Invoice + InvoiceLineItem tables."""
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0017_payment_method_config_and_allowed"),
    ]

    operations = [
        migrations.CreateModel(
            name="Invoice",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("number", models.CharField(db_index=True, max_length=40)),
                ("currency_code", models.CharField(default="USD", max_length=8)),
                ("subtotal", models.DecimalField(decimal_places=2, default=0,
                                                 max_digits=12)),
                ("tax_percent", models.DecimalField(
                    decimal_places=2, default=0, max_digits=5,
                    help_text="Tax applied to subtotal (0-100).")),
                ("tax_amount", models.DecimalField(decimal_places=2, default=0,
                                                    max_digits=12)),
                ("total", models.DecimalField(decimal_places=2, default=0,
                                              max_digits=12)),
                ("issue_date", models.DateField(auto_now_add=True)),
                ("due_date", models.DateField(blank=True, null=True)),
                ("client_snapshot", models.JSONField(blank=True, default=dict)),
                ("company_snapshot", models.JSONField(blank=True, default=dict)),
                ("payment_method_snapshot", models.JSONField(blank=True, default=dict)),
                ("general_description", models.TextField(
                    blank=True, default="",
                    help_text="Free-form description above the line items.")),
                ("notes", models.TextField(
                    blank=True, default="",
                    help_text="Short footer note, e.g. 'Thanks for your business.'")),
                ("status", models.CharField(
                    choices=[("draft", "Draft"), ("sent", "Sent"),
                             ("viewed", "Viewed"), ("paid", "Paid"),
                             ("void", "Void")],
                    db_index=True, default="draft", max_length=16)),
                ("share_token", models.CharField(
                    db_index=True, max_length=48, unique=True,
                    help_text="Random token for the public share link.")),
                ("expires_at", models.DateTimeField(
                    blank=True, null=True,
                    help_text="When the public link stops working. Null = never.")),
                ("first_viewed_at", models.DateTimeField(blank=True, null=True)),
                ("view_count", models.PositiveIntegerField(default=0)),
                ("sent_to_client_at", models.DateTimeField(blank=True, null=True)),
                ("sent_to_customer_at", models.DateTimeField(blank=True, null=True)),
                ("pdf_file", models.FileField(
                    blank=True, null=True, upload_to="invoices/pdf/")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="invoices", to="myapp.user")),
                ("client", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="invoices", to="myapp.client")),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="invoices", to="myapp.customercompany")),
                ("payment_method", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="invoices", to="myapp.paymentmethod",
                    help_text="The payment method the client should use. Can "
                              "be null if the customer has no allowed "
                              "methods — invoice is then generated without "
                              "a payment section.")),
            ],
            options={
                "db_table": "invoices",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(fields=["customer", "status"],
                               name="invoices_cust_status_idx"),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(fields=["customer", "-created_at"],
                               name="invoices_cust_created_idx"),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(fields=["client", "-created_at"],
                               name="invoices_client_created_idx"),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(fields=["company", "-created_at"],
                               name="invoices_company_created_idx"),
        ),
        migrations.CreateModel(
            name="InvoiceLineItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False)),
                ("position", models.PositiveSmallIntegerField(
                    default=0,
                    help_text="Display order within the invoice.")),
                ("name", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True, default="")),
                ("quantity", models.DecimalField(decimal_places=2, default=1,
                                                  max_digits=10)),
                ("unit_price", models.DecimalField(decimal_places=2, default=0,
                                                    max_digits=12)),
                ("total", models.DecimalField(decimal_places=2, default=0,
                                              max_digits=12)),
                ("invoice", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="line_items", to="myapp.invoice")),
            ],
            options={
                "db_table": "invoice_line_items",
                "ordering": ["position", "id"],
            },
        ),
    ]
