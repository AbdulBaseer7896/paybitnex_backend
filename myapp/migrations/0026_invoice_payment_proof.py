# Generated for invoice "payment received" proof + note feature.
#
# Adds three fields to Invoice so customers can attach a proof
# document and a note when marking an invoice as paid:
#   - payment_proof_file: optional uploaded receipt / screenshot
#   - payment_proof_note: optional free-form note from the customer
#   - paid_at:            timestamp captured at mark-as-paid time
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0025_merge_20260424_0422'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='payment_proof_file',
            field=models.FileField(
                blank=True, null=True,
                upload_to='invoices/payment_proof/',
                help_text='Document the customer uploaded as proof that '
                          'payment was received (screenshot, receipt PDF, '
                          'etc.).',
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='payment_proof_note',
            field=models.TextField(
                blank=True, default='',
                help_text="Customer's note/comment recorded when marking "
                          "the invoice as paid (e.g. 'Wire arrived "
                          "2026-04-25').",
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='paid_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='When the customer marked this invoice as paid.',
            ),
        ),
    ]
