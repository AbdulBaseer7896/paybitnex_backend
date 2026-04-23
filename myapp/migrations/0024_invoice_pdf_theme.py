# Generated for invoice dark-theme feature.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0023_customer_feature_access'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='pdf_theme',
            field=models.CharField(blank=True, default='light', max_length=16),
        ),
    ]
