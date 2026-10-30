"""
Production tenant-scoped operator views for CRM Bulk Connections and Follow-Ups.
Enforces workspace membership and role authorization on every view.
"""
import json
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404

from openoutreach.core.models import Workspace, WorkspaceMembership, Campaign, AuditEvent
from openoutreach.crm.models.lead import Lead, SuppressionRecord
from openoutreach.crm.models.deal import Deal
from openoutreach.crm.services.connection_engine import execute_connection_batch, ConnectionBatchTracker
from openoutreach.core.services.followup_engine import find_due_followups, execute_followup_batch


def _verify_workspace_access(request, workspace_id: int, required_roles=("owner", "admin", "member")):
    """Verifies user session membership and role for workspace."""
    if not request.user.is_authenticated:
        return None, HttpResponseForbidden("Authentication required")
    
    membership = WorkspaceMembership.objects.filter(
        workspace_id=workspace_id,
        user=request.user,
        status="active"
    ).first()

    if not membership or membership.role not in required_roles:
        return None, HttpResponseForbidden("Unauthorized workspace access")

    return membership.workspace, None


@login_required
@require_http_methods(["GET", "POST"])
def connection_batch_operator_view(request, workspace_id: int):
    """Operator view for Bulk Connections: preview, start, pause, resume, abort, results."""
    workspace, err = _verify_workspace_access(request, workspace_id)
    if err:
        return err

    if request.method == "GET":
        campaign_id = request.GET.get("campaign_id")
        campaign = get_object_or_404(Campaign, id=campaign_id, workspace=workspace)
        leads = Lead.objects.filter(workspace=workspace)[:50]

        previews = []
        for lead in leads:
            is_suppressed = SuppressionRecord.objects.filter(workspace=workspace, linkedin_url=lead.linkedin_url).exists()
            previews.append({
                "lead_id": lead.id,
                "name": f"{lead.first_name} {lead.last_name}",
                "linkedin_url": lead.linkedin_url,
                "blocked": is_suppressed,
                "block_reason": "suppressed" if is_suppressed else None
            })

        return JsonResponse({
            "workspace_id": workspace.id,
            "campaign_id": campaign.id,
            "target_count": len(previews),
            "targets": previews
        })

    elif request.method == "POST":
        data = json.loads(request.body or "{}")
        action = data.get("action", "start")
        campaign_id = data.get("campaign_id")
        campaign = get_object_or_404(Campaign, id=campaign_id, workspace=workspace)
        lead_ids = data.get("lead_ids", [])
        leads = list(Lead.objects.filter(workspace=workspace, id__in=lead_ids))

        res = execute_connection_batch(
            workspace=workspace,
            campaign=campaign,
            leads=leads,
            batch_id=data.get("batch_id", "op_batch_001")
        )
        return JsonResponse(res)


@login_required
@require_http_methods(["GET", "POST"])
def followup_batch_operator_view(request, workspace_id: int):
    """Operator view for Follow-Ups: preview due follow-ups, start, pause, resume, results."""
    workspace, err = _verify_workspace_access(request, workspace_id)
    if err:
        return err

    if request.method == "GET":
        campaign_id = request.GET.get("campaign_id")
        campaign = Campaign.objects.filter(id=campaign_id, workspace=workspace).first() if campaign_id else None
        due_deals = find_due_followups(workspace=workspace, campaign=campaign)

        return JsonResponse({
            "workspace_id": workspace.id,
            "due_count": len(due_deals),
            "due_deals": [{"deal_id": d.id, "lead_id": d.lead_id, "attempts": d.connect_attempts} for d in due_deals]
        })

    elif request.method == "POST":
        data = json.loads(request.body or "{}")
        campaign_id = data.get("campaign_id")
        campaign = get_object_or_404(Campaign, id=campaign_id, workspace=workspace)
        deal_ids = data.get("deal_ids", [])
        deals = list(Deal.objects.filter(campaign=campaign, id__in=deal_ids))

        res = execute_followup_batch(
            workspace=workspace,
            campaign=campaign,
            deals=deals,
            batch_id=data.get("batch_id", "followup_op_001")
        )
        return JsonResponse(res)
