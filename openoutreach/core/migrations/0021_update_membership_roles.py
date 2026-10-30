from django.db import migrations

def update_roles(apps, schema_editor):
    WorkspaceMembership = apps.get_model("core", "WorkspaceMembership")
    User = apps.get_model("auth", "User")

    # Set exact minimum justified role per user
    # 1. admin -> owner
    admin_u = User.objects.filter(username="admin").first()
    if admin_u:
        WorkspaceMembership.objects.filter(user=admin_u).update(role="owner", status="active")

    # 2. huntflow_admin -> admin
    h_admin_u = User.objects.filter(username="huntflow_admin").first()
    if h_admin_u:
        WorkspaceMembership.objects.filter(user=h_admin_u).update(role="admin", status="active")

    # 3. idwsdsjtasdkk -> owner if candidate-workspace else campaign_manager
    cand_u = User.objects.filter(username="idwsdsjtasdkk").first()
    if cand_u:
        WorkspaceMembership.objects.filter(user=cand_u, workspace__slug="candidate-workspace").update(role="owner", status="active")
        WorkspaceMembership.objects.filter(user=cand_u, workspace__slug="default").update(role="campaign_manager", status="active")

    # 4. staffop -> operator
    staff_u = User.objects.filter(username="staffop").first()
    if staff_u:
        WorkspaceMembership.objects.filter(user=staff_u).update(role="operator", status="active")

def rollback_roles(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0020_rename_idx_ws_user_status_ws_mem_ws_usr_stat_idx_and_more'),
    ]

    operations = [
        migrations.RunPython(update_roles, rollback_roles),
    ]
