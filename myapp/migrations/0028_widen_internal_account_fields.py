# Widen the account number / card number / IBAN fields on the
# internal-transactions reference tables.
#
# The columns originally held only "last 4 digits" — too restrictive
# in practice. Admins want to enter the full account number (up to
# 64 chars), full card number (up to 19 chars to cover Amex 15,
# Visa/MC 16, plus spaces), and a full IBAN (up to 64 chars).
#
# Field names are kept as-is to avoid a rename migration; only the
# max_length and help_text change.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0027_internal_transactions'),
    ]

    operations = [
        migrations.AlterField(
            model_name='usabankaccount',
            name='account_number_last4',
            field=models.CharField(
                blank=True, default='', max_length=64,
                help_text='Account number — full number or last digits, '
                          'your choice. Used for disambiguation in the picker.',
            ),
        ),
        migrations.AlterField(
            model_name='creditcard',
            name='last4',
            field=models.CharField(
                blank=True, default='', max_length=19,
                help_text='Card number — typically 15 (Amex) or 16 digits. '
                          'Spaces are allowed.',
            ),
        ),
        migrations.AlterField(
            model_name='internalpakistaniaccount',
            name='account_number_last4',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AlterField(
            model_name='internalpakistaniaccount',
            name='iban',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
