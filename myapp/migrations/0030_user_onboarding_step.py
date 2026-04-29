# Generated for onboarding step-resume feature.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0029_merge_20260428_0347'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='onboarding_step',
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
