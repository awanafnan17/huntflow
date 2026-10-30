from django.db import migrations

def clean_empty_legacy_workspace(apps, schema_editor):
    Workspace = apps.get_model("core", "Workspace")
    try:
        Lead = apps.get_model("crm", "Lead")
    except LookupError:
        Lead = None

    ws = Workspace.objects.filter(slug="default").first()
    if ws:
        mem_count = ws.memberships.count()
        camp_count = ws.campaigns.count()
        lead_count = Lead.objects.filter(workspace=ws).count() if Lead else 0
        if mem_count == 0 and camp_count == 0 and lead_count == 0:
            ws.delete()

def rollback_clean(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_seed_system_campaign_purposes'),
        ('crm', '0020_backfill_crm_workspace'),
    ]

    operations = [
        migrations.RunPython(clean_empty_legacy_workspace, rollback_clean),
    ]
