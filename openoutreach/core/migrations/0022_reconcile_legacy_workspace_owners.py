from django.db import migrations

def reconcile_workspace_owners(apps, schema_editor):
    Workspace = apps.get_model("core", "Workspace")
    WorkspaceMembership = apps.get_model("core", "WorkspaceMembership")
    User = apps.get_model("auth", "User")
    CandidateProfile = apps.get_model("career", "CandidateProfile")

    # 1. Locate candidate user deterministically via candidate profile if present
    prof = CandidateProfile.objects.order_by("id").first()
    cand_user = prof.user if prof else User.objects.filter(username="idwsdsjtasdkk").first()

    if cand_user:
        ws_cand, _ = Workspace.objects.get_or_create(
            slug="candidate-workspace",
            defaults={"name": "Candidate Workspace", "subscription_tier": "free", "status": "active"}
        )
        WorkspaceMembership.objects.update_or_create(
            workspace=ws_cand,
            user=cand_user,
            defaults={"role": "owner", "status": "active"}
        )

    # 2. Ensure every workspace has at least one active owner
    for ws in Workspace.objects.all():
        owners_count = WorkspaceMembership.objects.filter(workspace=ws, role="owner", status="active").count()
        if owners_count == 0:
            # Promote the earliest member to owner
            mem = WorkspaceMembership.objects.filter(workspace=ws).first()
            if mem:
                mem.role = "owner"
                mem.status = "active"
                mem.save()
            else:
                admin_user = User.objects.filter(is_superuser=True).first() or User.objects.first()
                if admin_user:
                    WorkspaceMembership.objects.create(workspace=ws, user=admin_user, role="owner", status="active")

def rollback_reconcile(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_update_membership_roles'),
        ('career', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(reconcile_workspace_owners, rollback_reconcile),
    ]
