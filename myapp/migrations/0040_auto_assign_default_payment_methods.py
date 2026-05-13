"""
Migration 0040: Support auto-assignment of default payment methods.

1. PaymentMethod gains no new fields (is_default already exists).
2. CustomerAllowedPaymentMethod gains `auto_assigned` bool flag
   (True when assigned automatically because is_default=True on the method)
   and `admin_excluded` bool flag (True when admin explicitly removed it
   for this customer — prevents re-assignment on subsequent runs).

Zero data loss: both fields are nullable/default, existing rows are
treated as manually assigned (auto_assigned=False, admin_excluded=False).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0039_backfill_real_exchange_rate"),
    ]

    operations = [
        migrations.AddField(
            model_name="customerallowedpaymentmethod",
            name="auto_assigned",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True when this grant was created automatically because the "
                    "payment method has is_default=True. False for admin-manually "
                    "assigned grants."
                ),
            ),
        ),
        migrations.AddField(
            model_name="customerallowedpaymentmethod",
            name="admin_excluded",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True when an admin explicitly removed this auto-assigned method "
                    "for this customer. Prevents re-auto-assignment when defaults run again."
                ),
            ),
        ),
    ]
