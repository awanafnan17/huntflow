# Data migration to invalidate pending legacy records with plaintext tokens but no valid hash
from django.db import migrations
from django.utils import timezone


def invalidate_legacy_tokens(apps, schema_editor):
    WorkspaceInvitation = apps.get_model("core", "WorkspaceInvitation")
    PasswordResetToken = apps.get_model("core", "PasswordResetToken")
    EmailVerificationToken = apps.get_model("core", "EmailVerificationToken")

    WorkspaceInvitation.objects.filter(token_hash__isnull=True, status="pending").update(
        status="revoked", revoked_at=timezone.now()
    )
    PasswordResetToken.objects.filter(token_hash__isnull=True, is_used=False).update(
        is_used=True, used_at=timezone.now()
    )
    EmailVerificationToken.objects.filter(token_hash__isnull=True, is_verified=False).update(
        expires_at=timezone.now()
    )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0033_add_token_hash_version"),
    ]

    operations = [
        migrations.RunPython(invalidate_legacy_tokens, migrations.RunPython.noop),
    ]
