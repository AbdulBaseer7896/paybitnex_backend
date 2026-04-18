from django.db import migrations, models


def auto_resolve_duplicates(apps, schema_editor):
    """
    Before applying the unique constraints, find any existing duplicate values
    and rename the extras in place so none remain. Strategy:
      - Keep the OLDEST row (by created_at) with its original value — it most
        likely has the most downstream references (OutgoingPKRTransfer,
        PartnerLedgerEntry, IncomingPayment via merchant_account, etc.).
      - Rename each subsequent duplicate by appending "-dup2", "-dup3", …
    No rows are deleted, so protected foreign keys stay intact. The renamed
    values are obviously-fake markers that are easy to spot and clean up
    later from the admin UI if you want to.
    """
    from django.db.models import Count

    def rename_dups(model, field, label):
        dup_values = (model.objects
                      .values(field)
                      .annotate(n=Count("id"))
                      .filter(n__gt=1)
                      .values_list(field, flat=True))
        for value in list(dup_values):
            rows = list(model.objects.filter(**{field: value}).order_by("created_at"))
            # Skip index 0 (oldest, keeps original value). Suffix the rest.
            for i, row in enumerate(rows[1:], start=2):
                new_val = f"{value}-dup{i}"
                # Extremely unlikely to collide, but guard anyway.
                while model.objects.filter(**{field: new_val}).exists():
                    i += 1
                    new_val = f"{value}-dup{i}"
                setattr(row, field, new_val)
                row.save(update_fields=[field])
                print(f"   [migration] renamed {label} '{value}' → '{new_val}' on id={row.pk}")

    BankAcct = apps.get_model("myapp", "CustomerBankAccount")
    MerchAcct = apps.get_model("myapp", "CustomerMerchantAccount")
    Payment = apps.get_model("myapp", "IncomingPayment")

    rename_dups(BankAcct, "account_number", "CustomerBankAccount.account_number")
    rename_dups(MerchAcct, "account_number", "CustomerMerchantAccount.account_number")
    rename_dups(Payment, "external_transaction_id", "IncomingPayment.external_transaction_id")


def noop_reverse(apps, schema_editor):
    # Nothing sensible to undo — renamed values are the new source of truth.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0008_user_profile_picture"),
    ]

    operations = [
        # Fix duplicate data BEFORE applying the unique constraint below.
        migrations.RunPython(auto_resolve_duplicates, noop_reverse),

        migrations.AlterField(
            model_name="customerbankaccount",
            name="account_number",
            field=models.CharField(
                db_index=True, max_length=50, unique=True,
                help_text="Must be unique across the whole system.",
            ),
        ),
        migrations.AlterField(
            model_name="customerbankaccount",
            name="iban",
            field=models.CharField(
                blank=True, max_length=50,
                help_text="IBAN (PK..) — unique if provided.",
            ),
        ),
        migrations.AlterField(
            model_name="customermerchantaccount",
            name="account_number",
            field=models.CharField(
                db_index=True, max_length=80, unique=True,
                help_text="Must be unique across the whole system.",
            ),
        ),
        migrations.AlterField(
            model_name="customermerchantaccount",
            name="iban",
            field=models.CharField(
                blank=True, max_length=80,
                help_text="IBAN / routing / SWIFT — unique if provided.",
            ),
        ),
        migrations.AlterField(
            model_name="incomingpayment",
            name="external_transaction_id",
            field=models.CharField(
                db_index=True, max_length=100, unique=True,
                help_text="Unique ID from sender's bank / platform — unique across all transactions",
            ),
        ),
        migrations.AlterField(
            model_name="incomingpayment",
            name="sender_company",
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name="incomingpayment",
            name="sender_bank_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AlterField(
            model_name="incomingpayment",
            name="sender_account_last4",
            field=models.CharField(blank=True, max_length=10),
        ),
    ]
