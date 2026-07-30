"""
0055 — Add PKR converter vendor linkage + enhanced VendorPKRPayment fields.

Changes:
  1. VendorPKRPayment.pk_bank_account   — FK to InternalPakistaniAccount (nullable)
  2. VendorPKRPayment.bank_transaction_id — CharField
  3. VendorPKRPayment.screenshot         — ImageField (nullable)
  4. InternalTransaction.pkr_converter_vendor — FK to Vendor (nullable)
  5. InternalTransaction.linked_vendor_pkr_payment — FK to VendorPKRPayment (nullable)
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0054_vendor_pkr_payments_and_card_bank_link"),
    ]

    operations = [
        # ── VendorPKRPayment enhancements ──────────────────────────────────
        migrations.AddField(
            model_name="vendorpkrpayment",
            name="pk_bank_account",
            field=models.ForeignKey(
                blank=True,
                help_text="Our internal Pakistani account where PKR was received.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="vendor_pkr_receipts",
                to="myapp.internalpakistaniaccount",
            ),
        ),
        migrations.AddField(
            model_name="vendorpkrpayment",
            name="bank_transaction_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Bank transaction reference / ID for this PKR transfer.",
                max_length=120,
            ),
        ),
        migrations.AddField(
            model_name="vendorpkrpayment",
            name="screenshot",
            field=models.ImageField(
                blank=True,
                help_text="Screenshot or proof of the PKR bank transfer.",
                null=True,
                upload_to="vendor_pkr_payments/screenshots/",
            ),
        ),

        # ── InternalTransaction PKR converter linkage ──────────────────────
        migrations.AddField(
            model_name="internaltransaction",
            name="pkr_converter_vendor",
            field=models.ForeignKey(
                blank=True,
                help_text="Person/vendor who converted USD to PKR for this transfer.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="converter_internal_transactions",
                to="myapp.vendor",
            ),
        ),
        migrations.AddField(
            model_name="internaltransaction",
            name="linked_vendor_pkr_payment",
            field=models.ForeignKey(
                blank=True,
                help_text="Auto-created VendorPKRPayment record linked to this transaction.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="source_internal_transactions",
                to="myapp.vendorpkrpayment",
            ),
        ),
    ]
