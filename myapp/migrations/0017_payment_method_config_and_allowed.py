"""
Turn 2: extend PaymentMethod with detail fields, seed Freight Flow
Solutions production defaults, and create the
CustomerAllowedPaymentMethod join table that controls which methods
each customer can offer on their invoices.
"""
from django.db import migrations, models
import django.db.models.deletion


def seed_payment_methods(apps, schema_editor):
    """
    Seed / update all four payment methods with the merchant's real
    receiving details. Uses `update_or_create` so running the migration
    again never duplicates rows, and admins who have already tweaked
    values see their edits preserved for fields we don't override.

    Data source — provided by the user (Bitnex Technologies / Freight
    Flow Solutions):

      Zelle     210-740-5653  / freightflowsol@gmail.com
                Freight Flow Solutions
      Cash App  $freightflow  / FreightFlowSolutionsHQ@gmail.com
                Joel Villanueva
      ACH/Wire  Acct 167504350833 / Rt 091000022
                Freight Flow Solutions, 100 LORENZ RD, SAN ANTONIO TX 78209
                US BANK · Business Checking
      Payoneer  Acct 400118255555 / Rt 124303243
                Freight Flow Solutions, 100 LORENZ RD, SAN ANTONIO TX 78209
                American Express National Bank · Business Checking
    """
    PaymentMethod = apps.get_model("myapp", "PaymentMethod")

    seeds = [
        {
            "code": "zelle",
            "defaults": {
                "label": "Zelle",
                "sort_order": 10,
                "is_active": True,
                "email": "freightflowsol@gmail.com",
                "phone": "210-740-5653",
                "holder_name": "Freight Flow Solutions",
                "instructions": "Send via Zelle using the email OR phone above. "
                                "Please reference the invoice number in the memo.",
            },
        },
        {
            "code": "cashapp",
            "defaults": {
                "label": "Cash App",
                "sort_order": 20,
                "is_active": True,
                "email": "FreightFlowSolutionsHQ@gmail.com",
                "cashapp_tag": "$freightflow",
                "holder_name": "Joel Villanueva",
                "instructions": "Send via Cash App using the $cashtag above. "
                                "Please reference the invoice number in the memo.",
            },
        },
        {
            "code": "ach_wire",
            "defaults": {
                "label": "ACH / Wire",
                "sort_order": 30,
                "is_active": True,
                "holder_name": "Freight Flow Solutions",
                "account_number": "167504350833",
                "routing_number": "091000022",
                "bank_name": "US BANK",
                "account_type": "Business Checking",
                "address_line1": "100 LORENZ RD",
                "city": "SAN ANTONIO",
                "state": "TX",
                "postal_code": "78209",
                "country": "USA",
                "instructions": "Send via ACH or Wire using the details above. "
                                "Please reference the invoice number in the memo.",
            },
        },
        {
            "code": "payoneer",
            "defaults": {
                "label": "Payoneer / Amex Bank",
                "sort_order": 40,
                "is_active": True,
                "holder_name": "Freight Flow Solutions",
                "account_number": "400118255555",
                "routing_number": "124303243",
                "bank_name": "American Express National Bank",
                "account_type": "Business Checking",
                "address_line1": "100 LORENZ RD",
                "city": "SAN ANTONIO",
                "state": "TX",
                "postal_code": "78209",
                "country": "USA",
                "instructions": "Send to the American Express National Bank "
                                "account above. Please reference the invoice "
                                "number in the memo.",
            },
        },
    ]

    for s in seeds:
        obj, created = PaymentMethod.objects.get_or_create(
            code=s["code"], defaults=s["defaults"],
        )
        # If the row existed but was missing new fields (older schema with
        # only code+label+is_active), backfill them. We use setattr +
        # save(update_fields) so we only touch fields that were actually
        # empty in the DB.
        if not created:
            changed = []
            for k, v in s["defaults"].items():
                current = getattr(obj, k, None)
                if not current:                      # empty string / None / 0 / False
                    setattr(obj, k, v)
                    changed.append(k)
            if changed:
                obj.save(update_fields=changed)


def unseed_payment_methods(apps, schema_editor):
    """Remove the seeded rows — only if no incoming payments reference them."""
    PaymentMethod = apps.get_model("myapp", "PaymentMethod")
    IncomingPayment = apps.get_model("myapp", "IncomingPayment")
    for code in ("zelle", "cashapp", "ach_wire", "payoneer"):
        if not IncomingPayment.objects.filter(payment_method_id=code).exists():
            PaymentMethod.objects.filter(code=code).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0016_invoicing_clients_companies"),
    ]

    operations = [
        # --- extend PaymentMethod ---
        migrations.AddField(
            model_name="paymentmethod",
            name="email",
            field=models.EmailField(blank=True, default="", max_length=254),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="phone",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="cashapp_tag",
            field=models.CharField(
                blank=True, default="", max_length=64,
                help_text="For Cash App, e.g. $freightflow"),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="holder_name",
            field=models.CharField(
                blank=True, default="", max_length=120,
                help_text="Account title / holder of record, e.g. 'Freight Flow Solutions'."),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="account_number",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="routing_number",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="bank_name",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="account_type",
            field=models.CharField(
                blank=True, default="", max_length=40,
                help_text="E.g. 'Business Checking'."),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="address_line1",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="address_line2",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="city",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="state",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="postal_code",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="country",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="qr_code",
            field=models.ImageField(
                blank=True, null=True,
                upload_to="payment_methods/qr/"),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="instructions",
            field=models.TextField(
                blank=True, default="",
                help_text="Extra instructions the client should follow, "
                          "e.g. 'Reference invoice # in memo'."),
        ),
        migrations.AddField(
            model_name="paymentmethod",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, null=True),
        ),

        # --- seed real Freight Flow data ---
        migrations.RunPython(seed_payment_methods, unseed_payment_methods),

        # --- CustomerAllowedPaymentMethod join table ---
        migrations.CreateModel(
            name="CustomerAllowedPaymentMethod",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False)),
                ("is_primary", models.BooleanField(default=False)),
                ("granted_at", models.DateTimeField(auto_now_add=True)),
                ("customer", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="allowed_payment_methods",
                    to="myapp.user")),
                ("payment_method", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="customer_grants",
                    to="myapp.paymentmethod")),
                ("granted_by", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="granted_payment_methods",
                    help_text="Admin/staff user who granted access.",
                    to="myapp.user")),
            ],
            options={
                "db_table": "customer_allowed_payment_methods",
                "ordering": ["-is_primary", "payment_method__sort_order"],
                "unique_together": {("customer", "payment_method")},
            },
        ),
        migrations.AddIndex(
            model_name="customerallowedpaymentmethod",
            index=models.Index(fields=["customer", "is_primary"],
                               name="cust_allow_pm_cust_prim_idx"),
        ),
    ]
