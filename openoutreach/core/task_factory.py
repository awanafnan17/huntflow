# openoutreach/core/task_factory.py
"""Central task factory for creating external-action tasks with target validation and idempotency keys."""
from __future__ import annotations

import logging
from typing import Any, Optional
from django.utils import timezone
from openoutreach.core.models import Task, TaskQueueEvent

logger = logging.getLogger(__name__)

LINKEDIN_CAMPAIGN_TASK_TYPES = {
    Task.TaskType.CONNECT,
    Task.TaskType.FOLLOW_UP,
    Task.TaskType.CHECK_PENDING,
    "check_pending",
    "message",
    "linkedin_message",
}


def _get_profile_workspace_id(prof) -> Optional[int]:
    """Resolve the workspace ID bound to a LinkedInProfile via explicit field or WorkspaceMembership."""
    if hasattr(prof, "workspace_id") and prof.workspace_id:
        return prof.workspace_id
    if getattr(prof, "user", None):
        try:
            from openoutreach.core.models import WorkspaceMembership
            m = WorkspaceMembership.objects.filter(user=prof.user, status="active").first()
            if m and m.workspace_id:
                return m.workspace_id
        except Exception:
            pass
    return None


def create_external_task(
    *,
    task_type: str | Task.TaskType,
    campaign_id: Optional[int] = None,
    target_id: Optional[int] = None,
    lead_id: Optional[int] = None,
    deal_id: Optional[int] = None,
    job_id: Optional[int] = None,
    candidate_id: Optional[int] = None,
    contact_id: Optional[int] = None,
    linkedin_profile_id: Optional[int] = None,
    scheduled_at: Optional[Any] = None,
    payload_extra: Optional[dict] = None,
) -> Optional[Task]:
    """Validates required target information and account-scoped idempotency before creating an external task."""
    from django.db import models
    task_type_str = str(task_type)
    if scheduled_at is None:
        scheduled_at = timezone.now()

    payload = {}
    if campaign_id is not None:
        payload["campaign_id"] = campaign_id
    if payload_extra:
        payload.update(payload_extra)

    # 1. Resolve linkedin_profile_id for LinkedIn campaign tasks
    if task_type_str in LINKEDIN_CAMPAIGN_TASK_TYPES or (campaign_id is not None and task_type_str in ["job_application", "application"]) or linkedin_profile_id is not None:
        profile_id = linkedin_profile_id or payload.get("linkedin_profile_id")
        try:
            from openoutreach.core.models import Campaign, WorkspaceMembership
            from openoutreach.linkedin.models import LinkedInProfile
            camp = Campaign.objects.filter(pk=campaign_id).first() if campaign_id else None
            if camp:
                camp_profile_id = camp.linkedin_profile_id
                if not camp_profile_id and camp.sender_identity and getattr(camp.sender_identity, "linkedin_profile_id", None):
                    camp_profile_id = camp.sender_identity.linkedin_profile_id

                if profile_id:
                    if camp_profile_id and profile_id != camp_profile_id:
                        profile_id = None
                else:
                    profile_id = camp_profile_id

            if profile_id:
                prof = LinkedInProfile.objects.filter(pk=profile_id, active=True).first()
                if not prof or not prof.can_execute(task_type_str):
                    profile_id = None
                elif camp and camp.workspace_id and camp.linkedin_profile_id != prof.id:
                    has_ws = WorkspaceMembership.objects.filter(user=prof.user, workspace_id=camp.workspace_id, status="active").exists()
                    if not has_ws:
                        profile_id = None
        except Exception as exc:
            logger.warning("Profile execution gate check exception: %s", exc)
            profile_id = None

        if not profile_id:
            if campaign_id is not None:
                _emit_missing_target_event(task_type_str, campaign_id, f"{task_type_str} task requires an authorized, bound linkedin_profile_id")
            return None
        payload["linkedin_profile_id"] = profile_id

    # 2. Target validation and account-scoped idempotency
    profile_id = payload.get("linkedin_profile_id", 0)
    effective_target_id = target_id

    if task_type_str == Task.TaskType.CONNECT:
        effective_target_id = lead_id or target_id
        if not effective_target_id:
            if campaign_id:
                _emit_missing_target_event(task_type_str, campaign_id, "CONNECT task requires lead_id")
            return None
        payload["lead_id"] = effective_target_id
        idempotency_key = f"connect:{profile_id}:{campaign_id}:{effective_target_id}"

    elif task_type_str == Task.TaskType.FOLLOW_UP:
        effective_target_id = deal_id or lead_id or target_id
        if not effective_target_id:
            if campaign_id:
                _emit_missing_target_event(task_type_str, campaign_id, "FOLLOW_UP task requires deal_id or lead_id")
            return None
        if deal_id:
            payload["deal_id"] = deal_id
        if lead_id:
            payload["lead_id"] = lead_id
        step = payload.get("follow_up_step", 1)
        idempotency_key = f"followup:{profile_id}:{campaign_id}:{effective_target_id}:{step}"

    elif task_type_str in ["application", "job_application", Task.TaskType.JOB_APPLICATION]:
        effective_target_id = job_id or target_id or payload.get("job_id")
        if not effective_target_id:
            if campaign_id:
                _emit_missing_target_event(task_type_str, campaign_id, "APPLICATION task requires job_id")
            return None
        payload["job_id"] = effective_target_id
        cand_id = candidate_id or payload.get("candidate_id", 0)
        payload["candidate_id"] = cand_id
        idempotency_key = f"job_application:{cand_id}:{effective_target_id}"

    elif task_type_str in [Task.TaskType.EMAIL, "message"]:
        effective_target_id = contact_id or lead_id or deal_id or target_id
        if not effective_target_id:
            if campaign_id:
                _emit_missing_target_event(task_type_str, campaign_id, f"{task_type_str} task requires contact/recipient ID")
            return None
        payload["contact_id"] = effective_target_id
        idempotency_key = f"{task_type_str.lower()}:{profile_id}:{campaign_id}:{effective_target_id}"
    else:
        idempotency_key = f"{task_type_str.lower()}:{profile_id}:{campaign_id}:{target_id or 'gen'}"

    payload["idempotency_key"] = idempotency_key

    # 3. Idempotency check: avoid duplicate pending or running tasks
    existing = Task.objects.filter(
        task_type=task_type_str,
        status__in=[Task.Status.PENDING, Task.Status.RUNNING],
    ).filter(
        models.Q(payload__idempotency_key=idempotency_key)
        | (models.Q(payload__candidate_id=payload.get("candidate_id")) & models.Q(payload__job_id=payload.get("job_id")) & models.Q(task_type=Task.TaskType.JOB_APPLICATION))
    ).first()
    if existing:
        logger.info("Duplicate external task blocked by idempotency key %s (existing Task #%d)", idempotency_key, existing.id)
        return existing

    # 4. Task Creation
    return Task.objects.create(
        task_type=task_type_str,
        scheduled_at=scheduled_at,
        payload=payload,
    )


def _emit_missing_target_event(task_type: str, campaign_id: int, reason: str) -> None:
    logger.warning("EXTERNAL_TASK_TARGET_MISSING for campaign #%d (%s): %s", campaign_id, task_type, reason)
    try:
        from openoutreach.core.models import Campaign
        camp = Campaign.objects.filter(pk=campaign_id).first()
        TaskQueueEvent.objects.create(
            campaign=camp,
            action="EXTERNAL_TASK_TARGET_MISSING",
            note=f"Blocked creation of targetless {task_type} task: {reason}",
            metadata={"campaign_id": campaign_id, "task_type": task_type, "reason": reason}
        )
    except Exception as exc:
        logger.error("Failed to emit missing target event: %s", exc)
