# Generated for the Internal Transactions feature.
#
# Adds five tables:
#   - internal_vendors            : admin-managed vendor list
#   - internal_usa_bank_accounts  : admin-managed USA bank accounts
#   - internal_credit_cards       : admin-managed credit cards
#   - internal_pk_accounts        : admin-managed Pakistani bank accounts
#   - internal_transactions       : the actual movement records,
#                                   linked to Expense for fee tracking
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0026_invoice_payment_proof'),
    ]

    operations = [
        # Vendor
        migrations.CreateModel(
            name='Vendor',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=200, unique=True)),
                ('contact_name', models.CharField(blank=True, default='', max_length=150)),
                ('contact_email', models.EmailField(blank=True, default='', max_length=254)),
                ('contact_phone', models.CharField(blank=True, default='', max_length=32)),
                ('notes', models.TextField(blank=True, default='')),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'internal_vendors',
                'ordering': ['name'],
            },
        ),

        # USABankAccount
        migrations.CreateModel(
            name='USABankAccount',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ('label', models.CharField(
                    help_text="Friendly label shown in the picker, e.g. 'Chase Business'.",
                    max_length=120,
                )),
                ('bank', models.CharField(
                    choices=[
                        ('us_bank', 'US Bank'),
                        ('amex', 'American Express'),
                        ('cashapp', 'Cash App'),
                        ('airwallex', 'Airwallex'),
                        ('chase', 'Chase'),
                        ('other', 'Other'),
                    ],
                    db_index=True, default='other', max_length=20,
                )),
                ('holder_name', models.CharField(blank=True, default='', max_length=150)),
                ('account_number_last4', models.CharField(
                    blank=True, default='',
                    help_text='Last 4 digits, for disambiguation in the picker.',
                    max_length=4,
                )),
                ('notes', models.TextField(blank=True, default='')),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'internal_usa_bank_accounts',
                'ordering': ['bank', 'label'],
            },
        ),

        # CreditCard
        migrations.CreateModel(
            name='CreditCard',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ('label', models.CharField(max_length=120)),
                ('brand', models.CharField(
                    choices=[
                        ('visa', 'Visa'),
                        ('mastercard', 'Mastercard'),
                        ('amex', 'American Express'),
                        ('discover', 'Discover'),
                        ('other', 'Other'),
                    ],
                    default='other', max_length=16,
                )),
                ('last4', models.CharField(blank=True, default='', max_length=4)),
                ('holder_name', models.CharField(blank=True, default='', max_length=150)),
                ('notes', models.TextField(blank=True, default='')),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'internal_credit_cards',
                'ordering': ['label'],
            },
        ),

        # InternalPakistaniAccount
        migrations.CreateModel(
            name='InternalPakistaniAccount',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ('label', models.CharField(max_length=150)),
                ('bank_name', models.CharField(max_length=150)),
                ('account_title', models.CharField(blank=True, default='', max_length=150)),
                ('account_number_last4', models.CharField(blank=True, default='', max_length=4)),
                ('iban', models.CharField(blank=True, default='', max_length=50)),
                ('notes', models.TextField(blank=True, default='')),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'internal_pk_accounts',
                'ordering': ['label'],
            },
        ),

        # InternalTransaction
        migrations.CreateModel(
            name='InternalTransaction',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ('source_type', models.CharField(
                    choices=[
                        ('usa_bank', 'USA bank account'),
                        ('credit_card', 'Credit card'),
                    ],
                    db_index=True, max_length=20,
                )),
                ('destination_type', models.CharField(
                    choices=[
                        ('usa_bank', 'USA bank account'),
                        ('vendor', 'Vendor'),
                        ('pk_bank', 'Pakistani bank account'),
                    ],
                    db_index=True, max_length=20,
                )),
                ('amount', models.DecimalField(
                    decimal_places=2, max_digits=18,
                    help_text='Gross amount transferred, in `currency`.',
                )),
                ('fee_amount', models.DecimalField(
                    decimal_places=2, default=0, max_digits=18,
                    help_text='Bank / wire / processing fee charged on this '
                              'transfer, in `currency`. Auto-pushed into Expenses '
                              'in the BANKING category.',
                )),
                ('method', models.CharField(
                    choices=[
                        ('wire', 'Wire'),
                        ('ach', 'ACH'),
                        ('card', 'Card'),
                        ('other', 'Other / internal'),
                    ],
                    db_index=True, default='other', max_length=16,
                )),
                ('reference', models.CharField(
                    blank=True, default='', max_length=120,
                    help_text='Bank reference / wire ID / memo line.',
                )),
                ('description', models.TextField(blank=True, default='')),
                ('occurred_on', models.DateField(
                    db_index=True,
                    help_text='Date the transaction was initiated.',
                )),
                ('document', models.FileField(
                    blank=True, null=True,
                    upload_to='internal_transactions/docs/',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='created_internal_transactions',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('currency', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='internal_transactions',
                    to='myapp.currency', to_field='code',
                )),
                ('fee_currency', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='internal_transaction_fees',
                    to='myapp.currency', to_field='code',
                    help_text='Currency the fee was charged in. Usually the same '
                              'as `currency`; left null = inherit from `currency`.',
                )),
                ('fee_expense', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='internal_transaction_fees',
                    to='myapp.expense',
                )),
                ('source_usa_bank', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='outgoing_transactions',
                    to='myapp.usabankaccount',
                )),
                ('source_credit_card', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='outgoing_transactions',
                    to='myapp.creditcard',
                )),
                ('dest_usa_bank', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='incoming_transactions',
                    to='myapp.usabankaccount',
                )),
                ('dest_vendor', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='payments_received',
                    to='myapp.vendor',
                )),
                ('dest_pk_bank', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='incoming_transactions',
                    to='myapp.internalpakistaniaccount',
                )),
            ],
            options={
                'db_table': 'internal_transactions',
                'ordering': ['-occurred_on', '-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='internaltransaction',
            index=models.Index(
                fields=['occurred_on', 'source_type'],
                name='internal_tx_occur_src_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='internaltransaction',
            index=models.Index(
                fields=['occurred_on', 'destination_type'],
                name='internal_tx_occur_dst_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='internaltransaction',
            index=models.Index(
                fields=['currency', 'occurred_on'],
                name='internal_tx_curr_occur_idx',
            ),
        ),
    ]
