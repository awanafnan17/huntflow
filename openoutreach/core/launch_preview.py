# openoutreach/core/launch_preview.py
from __future__ import annotations

import json
from django.utils import timezone
from openoutreach.core.models import Task, Campaign, SiteConfig
from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.core.readiness import check_launch_readiness
from openoutreach.dashboard.views import _mask_key


def mask_sensitive_payload_recursive(payload: dict | list) -> dict | list:
    """Recursively redact sensitive credential keys in payload structures."""
    sensitive_keys = {
        "password", "api_key", "token", "secret", "cookie", "authorization", 
        "credential", "smtp", "app_password"
    }
    if isinstance(payload, dict):
        masked = {}
        for k, v in payload.items():
            if any(s in k.lower() for s in sensitive_keys):
                masked[k] = "********"
            else:
                masked[k] = mask_sensitive_payload_recursive(v)
        return masked
    elif isinstance(payload, list):
        return [mask_sensitive_payload_recursive(item) for item in payload]
    else:
        return payload


def generate_launch_preview(campaign_id: int | None = None, limit: int = 5, task_type: str | None = None) -> dict:
    """Generate a read-only preview of what HuntFlow will execute upon daemon startup."""
    now = timezone.now()
    
    # 1. Reuse readiness checklist
    readiness = check_launch_readiness(campaign_id=campaign_id)
    
    # 2. Get pending executable tasks
    paused_ids = list(Campaign.objects.filter(is_active=False).values_list("id", flat=True))
    pending_executable_qs = Task.objects.filter(status=Task.Status.PENDING)
    if campaign_id is not None:
        pending_executable_list = [t for t in pending_executable_qs if t.payload.get("campaign_id") == campaign_id]
    else:
        pending_executable_list = [t for t in pending_executable_qs if t.payload.get("campaign_id") not in paused_ids]

    # Filter by task_type if provided
    if task_type:
        pending_executable_list = [t for t in pending_executable_list if t.task_type == task_type]

    # Sort by schedule date
    pending_executable_list.sort(key=lambda x: x.scheduled_at)
    
    # 3. Separate executable now vs future
    executable_now = []
    future_executable = []
    
    for t in pending_executable_list:
        if t.scheduled_at <= now:
            executable_now.append(t)
        else:
            future_executable.append(t)
            
    def serialize_task(task) -> dict:
        task_camp_id = task.payload.get("campaign_id")
        camp = Campaign.objects.filter(id=task_camp_id).first()
        camp_name = camp.name if camp else "Unknown"
        masked_payload = mask_sensitive_payload_recursive(task.payload)
        
        recipient_lead = None
        recipient_resolved = False
        resolved_count = 0
        note_or_follow_up_preview = None
        
        # 1. Try payload first
        lead_id = task.payload.get("lead_id") or task.payload.get("lead_public_id") or task.payload.get("public_id")
        if lead_id:
            if isinstance(lead_id, int):
                recipient_lead = Lead.objects.filter(id=lead_id).first()
            else:
                recipient_lead = Lead.objects.filter(public_identifier=lead_id).first()
            if recipient_lead:
                recipient_resolved = True
                resolved_count = 1
        
        # 2. If not in payload, query dynamically depending on task type
        if not recipient_resolved and camp:
            if task.task_type == "connect":
                ready_deals = Deal.objects.filter(campaign=camp, state=DealState.READY_TO_CONNECT).select_related("lead")
                resolved_count = ready_deals.count()
                if resolved_count == 1:
                    recipient_lead = ready_deals.first().lead
                    recipient_resolved = True
            elif task.task_type == "follow_up":
                from openoutreach.linkedin.tasks.follow_up import _connected_deals, _too_soon_to_nudge
                connected_deals = _connected_deals(camp)
                eligible_deals = [d for d in connected_deals if not _too_soon_to_nudge(d)]
                resolved_count = len(eligible_deals)
                if resolved_count == 1:
                    recipient_lead = eligible_deals[0].lead
                    recipient_resolved = True
            elif task.task_type == "check_pending":
                from django.utils import timezone
                due_deals = Deal.objects.filter(campaign=camp, state=DealState.PENDING, next_check_pending_at__lte=timezone.now()).select_related("lead")
                resolved_count = due_deals.count()
                if resolved_count == 1:
                    recipient_lead = due_deals.first().lead
                    recipient_resolved = True
                    
        # 3. Generate note/follow-up preview if resolved
        if recipient_resolved and recipient_lead and camp:
            if task.task_type == "connect" and camp.connection_note_enabled:
                from openoutreach.core.agents.connection_note import _generate_template_note
                try:
                    profile_dict = recipient_lead.to_profile_dict()
                    note_or_follow_up_preview = _generate_template_note(camp, profile_dict)
                except Exception as e:
                    note_or_follow_up_preview = f"Error generating template note: {e}"
            elif task.task_type == "follow_up":
                note_or_follow_up_preview = "[Follow-up message generated dynamically at runtime via agent]"

        recipient_info = {
            "resolved": recipient_resolved,
            "resolved_count": resolved_count,
            "lead_id": recipient_lead.id if recipient_lead else None,
            "name": f"{recipient_lead.first_name} {recipient_lead.last_name}".strip() if recipient_lead else None,
            "title": getattr(recipient_lead, "current_title", None) if recipient_lead else None,
            "company": getattr(recipient_lead, "company", None) if recipient_lead else None,
            "masked_linkedin_url": None,
            "note_preview": note_or_follow_up_preview,
        }
        
        if recipient_lead:
            pub_id = recipient_lead.public_identifier or ""
            if pub_id:
                masked_pub = pub_id[:min(len(pub_id), 8)] + "********"
                recipient_info["masked_linkedin_url"] = f"https://www.linkedin.com/in/{masked_pub}/"
            else:
                url = recipient_lead.linkedin_url or ""
                if url:
                    recipient_info["masked_linkedin_url"] = url[:min(len(url), 28)] + "********"

        return {
            "id": task.id,
            "task_type": task.task_type,
            "scheduled_at": str(task.scheduled_at),
            "campaign_id": task_camp_id,
            "campaign_name": camp_name,
            "payload_summary": masked_payload,
            "is_overdue": task.scheduled_at < now,
            "recipient_summary": recipient_info,
        }

    executable_now_serialized = [serialize_task(t) for t in executable_now[:limit]]
    future_executable_serialized = [serialize_task(t) for t in future_executable[:limit]]
    next_executable_tasks = [serialize_task(t) for t in pending_executable_list[:limit]]
        
    # Estimate launch outcome
    if readiness["overall_status"] == "blocked":
        estimated_outcome = "Launch BLOCKED. Daemon start is disabled. Blockers must be resolved first."
    elif readiness["overall_status"] == "warning":
        estimated_outcome = f"Daemon will start with warnings. There are {len(executable_now)} overdue task(s) ready to run immediately."
    else:
        estimated_outcome = f"Ready. Daemon will start successfully and execute {len(executable_now)} task(s) immediately."

    return {
        "readiness_status": readiness["overall_status"],
        "blockers": readiness["hard_blockers"],
        "warnings": readiness["warnings"],
        "next_action": readiness["next_action"],
        "next_executable_tasks": next_executable_tasks,
        "executable_now_tasks": executable_now_serialized,
        "future_executable_tasks": future_executable_serialized,
        "executable_now_count": len(executable_now),
        "future_executable_count": len(future_executable),
        "ignored_paused_count": readiness["sections"]["task_queue"]["ignored_pending"],
        "failed_tasks_count": readiness["sections"]["task_queue"]["failed_tasks"],
        "cancelled_tasks_count": readiness["sections"]["task_queue"]["cancelled_tasks"],
        "next_scheduled_time": readiness["sections"]["task_queue"]["next_scheduled_task"],
        "estimated_outcome": estimated_outcome,
        "campaign_scoped": campaign_id is not None,
        "scoped_campaign_id": campaign_id,
        "task_type_filter": task_type,
    }

