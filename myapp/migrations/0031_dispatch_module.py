"""Create DispatchCompany + DispatchDriver + Dispatch tables."""
from decimal import Decimal
import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0030_user_onboarding_step"),
    ]

    operations = [
        # ── DispatchCompany ──
        migrations.CreateModel(
            name="DispatchCompany",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                ("mc_number", models.CharField(
                    blank=True, default="", max_length=64,
                    help_text="MC / DOT identifier for the carrier (optional).")),
                ("contact_name", models.CharField(
                    blank=True, default="", max_length=150)),
                ("contact_email", models.EmailField(
                    blank=True, default="", max_length=254)),
                ("contact_phone", models.CharField(
                    blank=True, default="", max_length=32)),
                ("address", models.TextField(blank=True, default="")),
                ("default_dispatch_fee_percent", models.DecimalField(
                    decimal_places=2, default=Decimal("5.00"),
                    max_digits=5,
                    help_text="Default dispatch fee % charged on this company's loads.")),
                ("notes", models.TextField(blank=True, default="")),
                ("is_archived", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="dispatch_companies",
                    to="myapp.user")),
            ],
            options={
                "db_table": "dispatch_companies",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="dispatchcompany",
            index=models.Index(fields=["customer", "is_archived"],
                               name="dispatch_co_custome_arch_idx"),
        ),
        migrations.AddIndex(
            model_name="dispatchcompany",
            index=models.Index(fields=["customer", "name"],
                               name="dispatch_co_custome_name_idx"),
        ),
        # ── DispatchDriver ──
        migrations.CreateModel(
            name="DispatchDriver",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=150)),
                ("phone", models.CharField(
                    blank=True, default="", max_length=32)),
                ("email", models.EmailField(
                    blank=True, default="", max_length=254)),
                ("license_number", models.CharField(
                    blank=True, default="", max_length=64)),
                ("truck_type", models.CharField(
                    blank=True, default="", max_length=32,
                    choices=[
                        ("dry_van", "Dry Van"),
                        ("reefer", "Reefer"),
                        ("flatbed", "Flatbed"),
                        ("step_deck", "Step Deck"),
                        ("box_truck", "Box Truck"),
                        ("power_only", "Power Only"),
                        ("hotshot", "Hotshot"),
                        ("conestoga", "Conestoga"),
                        ("other", "Other"),
                    ])),
                ("truck_number", models.CharField(
                    blank=True, default="", max_length=64)),
                ("trailer_number", models.CharField(
                    blank=True, default="", max_length=64)),
                ("notes", models.TextField(blank=True, default="")),
                ("is_archived", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="dispatch_drivers",
                    to="myapp.user")),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="drivers",
                    to="myapp.dispatchcompany")),
            ],
            options={
                "db_table": "dispatch_drivers",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="dispatchdriver",
            index=models.Index(fields=["customer", "is_archived"],
                               name="dispatch_dr_custome_arch_idx"),
        ),
        migrations.AddIndex(
            model_name="dispatchdriver",
            index=models.Index(fields=["customer", "company"],
                               name="dispatch_dr_custome_comp_idx"),
        ),
        migrations.AddIndex(
            model_name="dispatchdriver",
            index=models.Index(fields=["company", "is_archived"],
                               name="dispatch_dr_company_arch_idx"),
        ),
        # ── Dispatch (loads) ──
        migrations.CreateModel(
            name="Dispatch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("company_name_snapshot", models.CharField(
                    blank=True, default="", max_length=200)),
                ("driver_name_snapshot", models.CharField(
                    blank=True, default="", max_length=150)),
                ("truck_type", models.CharField(
                    blank=True, default="", max_length=32,
                    choices=[
                        ("dry_van", "Dry Van"),
                        ("reefer", "Reefer"),
                        ("flatbed", "Flatbed"),
                        ("step_deck", "Step Deck"),
                        ("box_truck", "Box Truck"),
                        ("power_only", "Power Only"),
                        ("hotshot", "Hotshot"),
                        ("conestoga", "Conestoga"),
                        ("other", "Other"),
                    ])),
                ("broker_name", models.CharField(
                    blank=True, default="", max_length=200)),
                ("broker_phone", models.CharField(
                    blank=True, default="", max_length=64)),
                ("broker_email", models.EmailField(
                    blank=True, default="", max_length=254)),
                ("broker_mc", models.CharField(
                    blank=True, default="", max_length=64)),
                ("load_number", models.CharField(
                    blank=True, default="", max_length=64,
                    help_text="Load # assigned by the broker / shipper.")),
                ("booked_date", models.DateField(blank=True, null=True)),
                ("pickup_date", models.DateField(blank=True, null=True)),
                ("delivery_date", models.DateField(blank=True, null=True)),
                ("pickup_location", models.CharField(
                    blank=True, default="", max_length=255)),
                ("dropoff_location", models.CharField(
                    blank=True, default="", max_length=255)),
                ("extra_stops", models.TextField(blank=True, default="")),
                ("loaded_miles", models.PositiveIntegerField(default=0)),
                ("deadhead_miles", models.PositiveIntegerField(default=0)),
                ("rate", models.DecimalField(
                    decimal_places=2, default=Decimal("0.00"),
                    max_digits=12)),
                ("dispatch_fee_percent", models.DecimalField(
                    decimal_places=2, default=Decimal("0.00"),
                    max_digits=5)),
                ("dispatch_fee_flat", models.DecimalField(
                    decimal_places=2, default=Decimal("0.00"),
                    max_digits=10)),
                ("dispatch_fee", models.DecimalField(
                    decimal_places=2, default=Decimal("0.00"),
                    max_digits=10)),
                ("dispatcher_name", models.CharField(
                    blank=True, default="", max_length=150)),
                ("status", models.CharField(
                    db_index=True, default="booked", max_length=16,
                    choices=[
                        ("booked", "Booked"),
                        ("in_transit", "In Transit"),
                        ("delivered", "Delivered"),
                        ("paid", "Paid"),
                        ("cancelled", "Cancelled"),
                    ])),
                ("is_paid", models.BooleanField(db_index=True, default=False)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("notes", models.TextField(blank=True, default="")),
                ("rate_confirmation", models.FileField(
                    blank=True, null=True,
                    upload_to="dispatches/rate_confirmation/")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="dispatches",
                    to="myapp.user")),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="dispatches",
                    to="myapp.dispatchcompany")),
                ("driver", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="dispatches",
                    to="myapp.dispatchdriver")),
                ("invoice", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="dispatches",
                    to="myapp.invoice")),
            ],
            options={
                "db_table": "dispatches",
                "ordering": ["-pickup_date", "-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="dispatch",
            index=models.Index(fields=["customer", "status"],
                               name="dispatches_customer_stat_idx"),
        ),
        migrations.AddIndex(
            model_name="dispatch",
            index=models.Index(fields=["customer", "-pickup_date"],
                               name="dispatches_customer_pkup_idx"),
        ),
        migrations.AddIndex(
            model_name="dispatch",
            index=models.Index(fields=["customer", "is_paid"],
                               name="dispatches_customer_paid_idx"),
        ),
        migrations.AddIndex(
            model_name="dispatch",
            index=models.Index(fields=["company", "-pickup_date"],
                               name="dispatches_company_pkup_idx"),
        ),
        migrations.AddIndex(
            model_name="dispatch",
            index=models.Index(fields=["driver", "-pickup_date"],
                               name="dispatches_driver_pkup_idx"),
        ),
    ]
