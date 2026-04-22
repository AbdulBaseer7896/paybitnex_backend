"""Create Client + CustomerCompany tables (invoicing foundation)."""
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0015_stale_minutes_and_confirm_note"),
    ]

    operations = [
        migrations.CreateModel(
            name="Client",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                ("company_name", models.CharField(blank=True, default="", max_length=200)),
                ("email", models.EmailField(blank=True, default="", max_length=254)),
                ("phone", models.CharField(blank=True, default="", max_length=32)),
                ("address", models.TextField(blank=True, default="")),
                ("notes", models.TextField(blank=True, default="",
                                           help_text="Private notes the customer "
                                                     "keeps about this client. "
                                                     "Not rendered on invoices.")),
                ("is_archived", models.BooleanField(
                    default=False,
                    help_text="Soft-delete flag. Archived clients stay linked to "
                              "existing invoices but disappear from the picker.")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="clients",
                    to="myapp.user")),
            ],
            options={
                "db_table": "customer_clients",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="client",
            index=models.Index(fields=["customer", "is_archived"],
                               name="customer_cli_custome_b3d2a4_idx"),
        ),
        migrations.AddIndex(
            model_name="client",
            index=models.Index(fields=["customer", "name"],
                               name="customer_cli_custome_name_idx"),
        ),
        migrations.CreateModel(
            name="CustomerCompany",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                ("email", models.EmailField(blank=True, default="", max_length=254)),
                ("phone", models.CharField(blank=True, default="", max_length=32)),
                ("website", models.CharField(blank=True, default="", max_length=200)),
                ("address_line1", models.CharField(blank=True, default="", max_length=200)),
                ("address_line2", models.CharField(blank=True, default="", max_length=200)),
                ("city", models.CharField(blank=True, default="", max_length=100)),
                ("state", models.CharField(blank=True, default="", max_length=100)),
                ("postal_code", models.CharField(blank=True, default="", max_length=32)),
                ("country", models.CharField(blank=True, default="", max_length=100)),
                ("tax_id", models.CharField(
                    blank=True, default="", max_length=64,
                    help_text="EIN / VAT / GST number that should appear on "
                              "the invoice.")),
                ("logo", models.ImageField(
                    blank=True, null=True,
                    upload_to="customer_companies/logos/")),
                ("is_primary", models.BooleanField(default=False)),
                ("invoice_link_expiry_days", models.PositiveIntegerField(
                    blank=True, null=True,
                    help_text="Number of days after which a public invoice "
                              "link expires. Null means never expire.")),
                ("invoice_number_prefix", models.CharField(
                    blank=True, default="", max_length=16,
                    help_text="Optional prefix for auto-numbered invoices "
                              "issued from this company, e.g. 'ACME-'. Falls "
                              "back to a generic 'INV-' prefix when blank.")),
                ("next_invoice_number", models.PositiveIntegerField(
                    default=1,
                    help_text="Auto-incremented sequence for invoices issued "
                              "from this company.")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="companies",
                    to="myapp.user")),
            ],
            options={
                "db_table": "customer_companies",
                "ordering": ["-is_primary", "name"],
            },
        ),
        migrations.AddIndex(
            model_name="customercompany",
            index=models.Index(fields=["customer", "is_primary"],
                               name="customer_co_custome_prim_idx"),
        ),
    ]
