from django.db import migrations

def backfill_crm(apps, schema_editor):
    Workspace = apps.get_model("core", "Workspace")
    Lead = apps.get_model("crm", "Lead")
    SuppressionRecord = apps.get_model("crm", "SuppressionRecord")

    ws = Workspace.objects.filter(slug="default").first()
    if ws:
        Lead.objects.filter(workspace__isnull=True).update(workspace=ws)
        SuppressionRecord.objects.filter(workspace__isnull=True).update(workspace=ws)

def rollback_crm(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0019_lead_workspace_suppressionrecord_workspace'),
        ('core', '0018_backfill_legacy_workspace'),
    ]

    operations = [
        migrations.RunPython(backfill_crm, rollback_crm),
    ]
