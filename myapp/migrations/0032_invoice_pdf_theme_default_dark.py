from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Flip the default value of `Invoice.pdf_theme` from 'light' to
    'dark'. Newly created invoices will default to dark to match
    the modern dashboard styling.

    This migration only changes the field's default; existing rows
    keep whatever value they currently have. The PDF render helper
    has matching fallback logic, so old invoices without the column
    populated still render correctly.
    """

    dependencies = [
        ('myapp', '0031_dispatch_module'),
    ]

    operations = [
        migrations.AlterField(
            model_name='invoice',
            name='pdf_theme',
            field=models.CharField(blank=True, default='dark', max_length=16),
        ),
    ]
