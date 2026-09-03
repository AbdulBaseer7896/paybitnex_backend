from django.db import migrations

def set_accountants_verified(apps, schema_editor):
    User = apps.get_model('myapp', 'User')
    User.objects.filter(role="accountant").update(
        is_active=True,
        email_verified=True,
        verification_deadline=None
    )

def reverse_func(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0061_payment_stale_at_and_auto_confirm'),
    ]

    operations = [
        migrations.RunPython(set_accountants_verified, reverse_func),
    ]
