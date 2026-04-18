from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0007_kyc_objections"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="profile_picture",
            field=models.ImageField(blank=True, null=True, upload_to="avatars/"),
        ),
    ]
