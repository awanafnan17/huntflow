# openoutreach/core/services/data_operations.py
"""Customer Data Export & Account Deletion Lifecycle Management Service."""
from __future__ import annotations

from typing import Dict, Any
from django.utils import timezone
from django.db import transaction
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

from openoutreach.core.models import (
    Workspace,
    WorkspaceMembership,
    Campaign,
    SenderIdentity,
    CampaignMessageDraft,
    AuditEvent,
)
from openoutreach.crm.models.lead import Lead
from openoutreach.crm.models.deal import Deal
from openoutreach.core.services.audit_service import record_audit_event, sanitize_metadata


def export_workspace_data(workspace: Workspace, actor: User) -> Dict[str, Any]:
    """
    Generate a complete, structured JSON export of workspace data.
    Ensures sensitive keys are redacted.
    """
    memberships = list(workspace.memberships.values("id", "user__username", "user__email", "role", "status", "created_at"))
    senders = list(workspace.sender_identities.values("id", "display_name", "email", "sender_type", "verification_status", "is_active", "created_at"))
    campaigns = list(workspace.campaigns.values("id", "name", "campaign_type", "is_active"))
    leads = list(Lead.objects.for_workspace(workspace).values("id", "public_identifier", "first_name", "last_name", "headline", "company", "location", "api_email"))
    deals = list(Deal.objects.filter(campaign__workspace=workspace).values("id", "campaign_id", "lead_id", "state", "outcome"))
    drafts = list(CampaignMessageDraft.objects.for_workspace(workspace).values("id", "campaign_id", "lead_id", "status", "content_checksum", "created_at"))
    audits = list(AuditEvent.objects.for_workspace(workspace).values("id", "event_type", "result", "created_at"))

    raw_export = {
        "workspace": {
            "id": workspace.id,
            "name": workspace.name,
            "slug": workspace.slug,
            "status": workspace.status,
            "created_at": str(workspace.created_at),
        },
        "memberships": memberships,
        "sender_identities": senders,
        "campaigns": campaigns,
        "leads": leads,
        "deals": deals,
        "message_drafts": drafts,
        "audit_events": audits,
    }

    sanitized_export = sanitize_metadata(raw_export)

    record_audit_event(
        workspace=workspace,
        event_type="workspace_exported",
        actor=actor,
        target=workspace,
        action_key=f"exp_{workspace.id}_{int(timezone.now().timestamp())}",
        metadata={"record_count": len(leads) + len(campaigns)},
    )
    return sanitized_export


def delete_workspace_with_retention(workspace: Workspace, actor: User) -> Workspace:
    """
    Initiate soft deletion with 30-day retention lock.
    """
    workspace.status = "pending_deletion"
    workspace.save(update_fields=["status"])

    record_audit_event(
        workspace=workspace,
        event_type="workspace_deletion_requested",
        actor=actor,
        target=workspace,
        action_key=f"del_req_{workspace.id}",
        metadata={"retention_days": 30},
    )
    return workspace


@transaction.atomic
def delete_user_account(user: User, actor: User) -> User:
    """
    Safely delete or deactivate user account.
    Fails closed if user is sole owner of an active workspace.
    """
    # Check if user is sole owner of any active workspace
    owned_memberships = WorkspaceMembership.objects.filter(user=user, role="owner", status="active")
    for mem in owned_memberships:
        ws = mem.workspace
        other_owners = WorkspaceMembership.objects.filter(workspace=ws, role="owner", status="active").exclude(user=user).count()
        if other_owners == 0 and ws.status == "active":
            raise ValidationError(f"Cannot delete user who is sole owner of active workspace '{ws.name}'. Transfer ownership first.")

    user.is_active = False
    user.save()

    # Soft delete memberships
    user.workspace_memberships.update(status="suspended")

    for mem in user.workspace_memberships.all():
        record_audit_event(
            workspace=mem.workspace,
            event_type="account_deletion_requested",
            actor=actor,
            target=user,
            action_key=f"usr_del_{user.id}_{mem.workspace.id}",
            metadata={"user_id": user.id, "username": user.username},
        )

    return user
