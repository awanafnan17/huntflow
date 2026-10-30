# Generated manually for Staged Token Migration C
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0034_invalidate_legacy_plaintext_tokens"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="workspaceinvitation",
            name="token",
        ),
        migrations.RemoveField(
            model_name="passwordresettoken",
            name="token",
        ),
        migrations.RemoveField(
            model_name="emailverificationtoken",
            name="token",
        ),
        migrations.AlterField(
            model_name="workspaceinvitation",
            name="token_hash",
            field=models.CharField(db_index=True, max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name="passwordresettoken",
            name="token_hash",
            field=models.CharField(db_index=True, max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name="emailverificationtoken",
            name="token_hash",
            field=models.CharField(db_index=True, max_length=64, unique=True),
        ),
    ]
