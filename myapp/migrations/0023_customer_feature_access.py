# Generated for premium feature-gating system.
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0022_alter_invoicepaymentmethod_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='CustomerFeatureAccess',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ('feature_key', models.CharField(
                    db_index=True, max_length=64,
                    help_text="String key from the FEATURES registry (e.g. 'invoicing').")),
                ('enabled', models.BooleanField(default=True)),
                ('notes', models.TextField(
                    blank=True,
                    help_text='Optional admin notes — why access was granted/revoked.')),
                ('granted_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('granted_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=models.deletion.SET_NULL,
                    related_name='features_granted',
                    to=settings.AUTH_USER_MODEL)),
                ('user', models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name='feature_grants',
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'customer_feature_access',
                'unique_together': {('user', 'feature_key')},
            },
        ),
        migrations.AddIndex(
            model_name='customerfeatureaccess',
            index=models.Index(
                fields=['user', 'feature_key'],
                name='cust_feat_user_key_idx'),
        ),
        migrations.AddIndex(
            model_name='customerfeatureaccess',
            index=models.Index(
                fields=['feature_key', 'enabled'],
                name='cust_feat_key_en_idx'),
        ),
    ]
