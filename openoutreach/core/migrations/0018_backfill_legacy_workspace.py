from django.db import migrations

def backfill_workspaces(apps, schema_editor):
    Workspace = apps.get_model("core", "Workspace")
    WorkspaceMembership = apps.get_model("core", "WorkspaceMembership")
    Campaign = apps.get_model("core", "Campaign")
    User = apps.get_model("auth", "User")

    # 1. Create default workspace if none exists
    ws, created = Workspace.objects.get_or_create(
        slug="default",
        defaults={
            "name": "Default Workspace",
            "subscription_tier": "free",
            "status": "active"
        }
    )

    # 2. Make all existing users owners of the default workspace
    for user in User.objects.all():
        WorkspaceMembership.objects.get_or_create(
            workspace=ws,
            user=user,
            defaults={"role": "owner"}
        )

    # 3. Point all existing campaigns to the default workspace
    Campaign.objects.filter(workspace__isnull=True).update(workspace=ws)

def rollback_workspaces(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_workspace_campaign_workspace_workspacemembership'),
    ]

    operations = [
        migrations.RunPython(backfill_workspaces, rollback_workspaces),
    ]
