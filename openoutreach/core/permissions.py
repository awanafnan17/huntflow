# openoutreach/core/permissions.py
from django.core.exceptions import PermissionDenied
from openoutreach.core.models import WorkspaceMembership

ROLE_PERMISSIONS = {
    "owner": {
        "workspace.manage",
        "members.manage",
        "campaign.create",
        "campaign.edit",
        "target.import",
        "draft.generate",
        "draft.review",
        "draft.approve",
        "queue.authorize",
        "queue.execute",
        "integration.manage",
        "billing.manage",
        "audit.view",
        "data.export",
        "workspace.delete",
    },
    "admin": {
        "members.manage",
        "campaign.create",
        "campaign.edit",
        "target.import",
        "draft.generate",
        "draft.review",
        "draft.approve",
        "queue.authorize",
        "queue.execute",
        "integration.manage",
    },
    "campaign_manager": {
        "campaign.create",
        "campaign.edit",
        "target.import",
        "draft.generate",
        "draft.review",
        "audit.view",
    },
    "reviewer": {
        "draft.review",
        "draft.approve",
    },
    "operator": {
        "queue.execute",
    },
    "viewer": {
        "audit.view",
    },
}

def get_workspace_for_user(user, workspace_slug=None, workspace_id=None):
    """
    Finds and returns the active workspace for the user, or raises PermissionDenied.
    """
    if not user.is_authenticated:
        raise PermissionDenied("User is not authenticated.")
    
    filters = {"user": user, "status": "active", "workspace__status": "active"}
    if workspace_slug:
        filters["workspace__slug"] = workspace_slug
    elif workspace_id:
        filters["workspace_id"] = workspace_id
        
    membership = WorkspaceMembership.objects.filter(**filters).select_related("workspace").first()
    if not membership:
        raise PermissionDenied("User does not have an active membership in the requested workspace.")
    return membership.workspace, membership.role

def has_workspace_permission(user, workspace, permission):
    """
    Checks if a user has the specified permission in a workspace.
    """
    if not user or not user.is_authenticated:
        return False
    membership = WorkspaceMembership.objects.filter(workspace=workspace, user=user).first()
    if not membership or membership.status != "active":
        return False
    if workspace.status != "active":
        return False
    
    allowed = ROLE_PERMISSIONS.get(membership.role, set())
    return permission in allowed

def require_workspace_permission(user, workspace, permission):
    """
    Enforces that a user has a specific permission in a workspace, raising PermissionDenied if not.
    """
    if not has_workspace_permission(user, workspace, permission):
        raise PermissionDenied(f"Required permission '{permission}' is missing.")

def require_workspace_membership(user, workspace):
    """
    Ensures user is an active member of workspace.
    """
    if not user or not user.is_authenticated:
        raise PermissionDenied("User is not authenticated.")
    membership = WorkspaceMembership.objects.filter(workspace=workspace, user=user, status="active").first()
    if not membership or workspace.status != "active":
        raise PermissionDenied("User is not an active member of this workspace.")
    return membership

def get_workspace_object_or_404(model_cls, pk, workspace):
    """
    Safely retrieves an object by pk scoped to a workspace, or raises Http404/ObjectDoesNotExist.
    """
    from django.shortcuts import get_object_or_404
    # Check if model has direct workspace field
    if hasattr(model_cls, "workspace"):
        return get_object_or_404(model_cls, pk=pk, workspace=workspace)
    # Check if model has campaign field (like Deal)
    elif hasattr(model_cls, "campaign"):
        return get_object_or_404(model_cls, pk=pk, campaign__workspace=workspace)
    else:
        return get_object_or_404(model_cls, pk=pk)

