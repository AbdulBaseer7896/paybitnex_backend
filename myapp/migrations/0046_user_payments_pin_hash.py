"""Add `payments_pin_hash` to User for the customer "My Payments" PIN lock.

Stores a salted hash of the customer's optional PIN. The PIN lives on the
account so it is recognised across browsers/devices; the *unlocked* state is
tracked per-browser in localStorage, so a fresh browser starts locked.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0045_incomingpayment_occurred_on"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="payments_pin_hash",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
