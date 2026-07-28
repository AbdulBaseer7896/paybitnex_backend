import uuid

from django.db import migrations, models
import django.db.models.deletion


def link_chase_card_to_bank(apps, schema_editor):
    USABankAccount = apps.get_model("myapp", "USABankAccount")
    CreditCard = apps.get_model("myapp", "CreditCard")

    chase_bank = (
        USABankAccount.objects.filter(bank="chase", is_active=True)
        .order_by("created_at")
        .first()
    )
    if chase_bank:
        CreditCard.objects.filter(
            label__icontains="chase", linked_usa_bank__isnull=True,
        ).update(linked_usa_bank=chase_bank)


class Migration(migrations.Migration):
    dependencies = [
        ("myapp", "0053_vendor_portal_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendor",
            name="handles_pkr_conversion",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=(
                    "This person/vendor can receive USD and settle "
                    "the company in PKR."
                ),
            ),
        ),
        migrations.AddField(
            model_name="creditcard",
            name="linked_usa_bank",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Bank account that issues/settles this card "
                    "(for example Chase)."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="linked_credit_cards",
                to="myapp.usabankaccount",
            ),
        ),
        migrations.CreateModel(
            name="VendorPKRPayment",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "pkr_received",
                    models.DecimalField(decimal_places=2, max_digits=20),
                ),
                (
                    "usd_sent",
                    models.DecimalField(decimal_places=2, max_digits=18),
                ),
                (
                    "exchange_rate",
                    models.DecimalField(
                        blank=True,
                        decimal_places=6,
                        help_text=(
                            "PKR per USD. If omitted, the current USD "
                            "exchange rate is applied."
                        ),
                        max_digits=14,
                        null=True,
                    ),
                ),
                (
                    "pkr_equivalent",
                    models.DecimalField(
                        decimal_places=2,
                        editable=False,
                        help_text=(
                            "USD sent multiplied by the applied exchange rate."
                        ),
                        max_digits=20,
                    ),
                ),
                (
                    "balance_pkr",
                    models.DecimalField(
                        decimal_places=2,
                        editable=False,
                        help_text=(
                            "PKR received minus the PKR equivalent of USD sent."
                        ),
                        max_digits=20,
                    ),
                ),
                (
                    "confirmation_code",
                    models.CharField(blank=True, default="", max_length=120),
                ),
                ("notes", models.TextField(blank=True, default="")),
                ("occurred_on", models.DateField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_vendor_pkr_payments",
                        to="myapp.user",
                    ),
                ),
                (
                    "vendor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="pkr_payments",
                        to="myapp.vendor",
                    ),
                ),
            ],
            options={
                "db_table": "vendor_pkr_payments",
                "ordering": ["-occurred_on", "-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="vendorpkrpayment",
            index=models.Index(
                fields=["vendor", "occurred_on"],
                name="vendor_pkr__vendor__e4cf61_idx",
            ),
        ),
        migrations.RunPython(
            link_chase_card_to_bank,
            migrations.RunPython.noop,
        ),
    ]
