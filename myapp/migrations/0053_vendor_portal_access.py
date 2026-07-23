"""
Migration 0053: Vendor portal access.

Adds four columns to `internal_vendors` so an existing customer account
can be linked to a vendor and given read-only visibility of the card
transactions we paid them:

    portal_user        OneToOne -> users   (null = no portal access)
    portal_enabled     bool, default False (master switch)
    portal_granted_at  when access was granted
    portal_granted_by  which admin granted it

PURELY ADDITIVE
---------------
Every column is nullable or has a False/None default, so existing vendor
rows are untouched and behave exactly as before: `portal_user` is NULL
and `portal_enabled` is False, which means no portal access. No data
backfill is needed or performed.

SET_NULL on both FKs is deliberate. Deleting a user must never cascade
into deleting a Vendor row — a vendor carries financial history, and
removing a login should not remove the record of what we paid them.
Access simply drops (see `Vendor.has_portal_access`, which requires a
live linked user).

The OneToOne on `portal_user` gives a database-level guarantee that one
login maps to at most one vendor, which is what makes the portal's
scoping rule auditable.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0052_incomingpayment_is_rate_provisional"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendor",
            name="portal_user",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="vendor_profile",
                to="myapp.user",
                help_text=(
                    "Customer account granted vendor-portal access to this "
                    "vendor's card transactions. Null = no portal access."
                ),
            ),
        ),
        migrations.AddField(
            model_name="vendor",
            name="portal_enabled",
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text=(
                    "Master switch for this vendor's portal access. Set "
                    "False to revoke immediately without unlinking the "
                    "account."
                ),
            ),
        ),
        migrations.AddField(
            model_name="vendor",
            name="portal_granted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="vendor",
            name="portal_granted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="granted_vendor_portals",
                to="myapp.user",
            ),
        ),
    ]
