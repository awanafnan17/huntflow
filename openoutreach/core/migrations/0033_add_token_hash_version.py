# Generated manually for Staged Token Migration A
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0032_syntheticdemonstrationrun_auditevent_action_key_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspaceinvitation",
            name="token_hash_version",
            field=models.CharField(default="v1", max_length=16),
        ),
        migrations.AddField(
            model_name="passwordresettoken",
            name="token_hash_version",
            field=models.CharField(default="v1", max_length=16),
        ),
        migrations.AddField(
            model_name="emailverificationtoken",
            name="token_hash_version",
            field=models.CharField(default="v1", max_length=16),
        ),
    ]
