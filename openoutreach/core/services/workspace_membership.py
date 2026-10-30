# openoutreach/core/services/workspace_membership.py
from django.db import transaction
from django.core.exceptions import ValidationError
from openoutreach.core.models import WorkspaceMembership, Workspace

@transaction.atomic
def create_workspace_with_owner(name: str, slug: str, owner_user) -> Workspace:
    """Creates a workspace and immediately creates an active owner membership."""
    ws = Workspace.objects.create(name=name, slug=slug)
    WorkspaceMembership.objects.create(workspace=ws, user=owner_user, role="owner", status="active")
    return ws

@transaction.atomic
def change_workspace_member_role(workspace: Workspace, target_user, new_role: str, actor=None) -> WorkspaceMembership:
    """Changes a member's role in a workspace safely, guarding the last owner invariant."""
    if new_role not in dict(WorkspaceMembership.ROLE_CHOICES):
        raise ValidationError(f"Invalid role: {new_role}")

    membership = WorkspaceMembership.objects.select_for_update().filter(
        workspace=workspace, user=target_user
    ).first()
    if not membership:
        raise ValidationError("Target user is not a member of this workspace.")

    if membership.role == "owner" and new_role != "owner":
        owner_count = WorkspaceMembership.objects.filter(
            workspace=workspace, role="owner", status="active"
        ).count()
        if owner_count <= 1:
            raise ValidationError("The last owner cannot be demoted.")

    membership.role = new_role
    membership.full_clean()
    membership.save()
    return membership

@transaction.atomic
def remove_workspace_member(workspace: Workspace, target_user, actor=None):
    """Removes a member from a workspace safely, guarding the last owner invariant."""
    membership = WorkspaceMembership.objects.select_for_update().filter(
        workspace=workspace, user=target_user
    ).first()
    if not membership:
        raise ValidationError("Target user is not a member of this workspace.")

    if membership.role == "owner":
        owner_count = WorkspaceMembership.objects.filter(
            workspace=workspace, role="owner", status="active"
        ).count()
        if owner_count <= 1:
            raise ValidationError("The last owner cannot be removed.")

    membership.delete()

@transaction.atomic
def leave_workspace(workspace: Workspace, user_leaving):
    """Allows a user to leave a workspace, guarding the last owner invariant."""
    remove_workspace_member(workspace, user_leaving, actor=user_leaving)

@transaction.atomic
def suspend_workspace_member(workspace: Workspace, target_user, actor=None) -> WorkspaceMembership:
    """Suspends a workspace member, guarding the last owner invariant."""
    membership = WorkspaceMembership.objects.select_for_update().filter(
        workspace=workspace, user=target_user
    ).first()
    if not membership:
        raise ValidationError("Target user is not a member of this workspace.")

    if membership.role == "owner":
        owner_count = WorkspaceMembership.objects.filter(
            workspace=workspace, role="owner", status="active"
        ).count()
        if owner_count <= 1:
            raise ValidationError("The last owner cannot be suspended.")

    membership.status = "suspended"
    membership.full_clean()
    membership.save()
    return membership

@transaction.atomic
def transfer_workspace_ownership(workspace: Workspace, current_owner, new_owner, actor=None):
    """Transfers primary workspace ownership by promoting new_owner to owner first, then demoting current_owner."""
    new_mem = WorkspaceMembership.objects.select_for_update().filter(
        workspace=workspace, user=new_owner
    ).first()
    if not new_mem:
        raise ValidationError("New owner must be a member of the workspace first.")

    curr_mem = WorkspaceMembership.objects.select_for_update().filter(
        workspace=workspace, user=current_owner
    ).first()
    if not curr_mem or curr_mem.role != "owner":
        raise ValidationError("Current user is not an owner of the workspace.")

    # Promote new owner first
    new_mem.role = "owner"
    new_mem.status = "active"
    new_mem.save()

    # Demote current owner
    curr_mem.role = "admin"
    curr_mem.save()
