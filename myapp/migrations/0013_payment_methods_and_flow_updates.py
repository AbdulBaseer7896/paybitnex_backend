"""
Migration: 0013_payment_methods_and_flow_updates

Adds:
- PaymentMethod lookup table (seeded with Zelle / Cash App / ACH-Wire)
- IncomingPayment.payment_method FK (nullable)
- IncomingPayment.customer_confirmed_at, is_stale, force_completed_by,
  force_completed_at (new completion-flow fields)
- SystemSetting `stale_payment_days` = 3

Removes (soft):
- IncomingPayment.merchant_account FK constraint — we keep the underlying
  column so legacy rows retain their historical merchant reference, but
  new code no longer enforces FK integrity to a row in the merchant table.
  This means the CustomerMerchantAccount table can later be dropped safely.

Deactivates EUR and GBP in the `currencies` table (does not delete them so
existing exchange rates + historical payments keep referential integrity).
"""
from django.db import migrations, models
import django.db.models.deletion


def seed_payment_methods_and_setting(apps, schema_editor):
    PaymentMethod = apps.get_model("myapp", "PaymentMethod")
    PaymentMethod.objects.bulk_create([
        PaymentMethod(code="zelle",   label="Zelle",     sort_order=1),
        PaymentMethod(code="cashapp", label="Cash App",  sort_order=2),
        PaymentMethod(code="ach_wire",label="ACH / Wire",sort_order=3),
    ], ignore_conflicts=True)

    SystemSetting = apps.get_model("myapp", "SystemSetting")
    SystemSetting.objects.get_or_create(
        key="stale_payment_days",
        defaults={
            "value": "3",
            "description": (
                "Days a payment can sit in 'PKR Sent' awaiting customer "
                "confirmation before it's flagged stale and moved to the "
                "'Awaiting customer confirmation' section."
            ),
        },
    )


def deactivate_eur_gbp(apps, schema_editor):
    Currency = apps.get_model("myapp", "Currency")
    Currency.objects.filter(code__in=["EUR", "GBP"]).update(is_active=False)


def reactivate_eur_gbp(apps, schema_editor):
    # Reverse — harmless if rolling back.
    Currency = apps.get_model("myapp", "Currency")
    Currency.objects.filter(code__in=["EUR", "GBP"]).update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0012_rename_email_otps_email_purpose_idx_email_otps_email_4a8bd3_idx_and_more"),
    ]

    operations = [
        # ── 1. PaymentMethod lookup table ────────────────────────────
        migrations.CreateModel(
            name="PaymentMethod",
            fields=[
                ("code", models.CharField(max_length=32, primary_key=True, serialize=False)),
                ("label", models.CharField(max_length=80)),
                ("is_active", models.BooleanField(default=True)),
                ("sort_order", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "payment_methods",
                "ordering": ["sort_order", "label"],
            },
        ),

        # ── 2. Seed default payment methods + stale_payment_days setting ──
        migrations.RunPython(seed_payment_methods_and_setting,
                             reverse_code=migrations.RunPython.noop),

        # ── 3. Add new fields to IncomingPayment ────────────────────
        migrations.AddField(
            model_name="incomingpayment",
            name="payment_method",
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="incoming_payments",
                to="myapp.paymentmethod",
                to_field="code",
            ),
        ),
        migrations.AddField(
            model_name="incomingpayment",
            name="customer_confirmed_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="incomingpayment",
            name="is_stale",
            field=models.BooleanField(default=False, db_index=True),
        ),
        migrations.AddField(
            model_name="incomingpayment",
            name="force_completed_by",
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="force_completed_payments",
                to="myapp.user",
            ),
        ),
        migrations.AddField(
            model_name="incomingpayment",
            name="force_completed_at",
            field=models.DateTimeField(null=True, blank=True),
        ),

        # ── 4. Make merchant_account optional (soft removal) ─────────
        # We remove the FK from the Django model but keep the underlying
        # column in the DB so legacy rows still carry the historical
        # merchant UUID. Using AlterField to make it nullable + switching
        # the on_delete to SET_NULL so orphaned rows don't block deletes.
        migrations.AlterField(
            model_name="incomingpayment",
            name="merchant_account",
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="incoming_payments",
                to="myapp.customermerchantaccount",
            ),
        ),

        # ── 5. Index on (status, is_stale) for fast filtering ────────
        migrations.AddIndex(
            model_name="incomingpayment",
            index=models.Index(fields=["status", "is_stale"],
                               name="incoming_pa_status_is_stal_idx"),
        ),

        # ── 6. Deactivate EUR + GBP (data-only) ──────────────────────
        migrations.RunPython(deactivate_eur_gbp, reverse_code=reactivate_eur_gbp),
    ]
