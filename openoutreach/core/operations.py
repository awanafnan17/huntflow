# openoutreach/core/operations.py
from __future__ import annotations

import json
from django.utils import timezone
from datetime import timedelta
from openoutreach.core.models import Task, Campaign, DaemonStatus, TaskQueueEvent, LaunchRun, LaunchRunEvent, SiteConfig
from openoutreach.crm.models import Lead, Deal, DealState, LeadPipelineEvent
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.core.readiness import check_launch_readiness, is_pid_alive


def mask_sensitive_payload_recursive(payload: dict | list) -> dict | list:
    """Recursively redact sensitive credential keys in payload structures."""
    sensitive_keys = {
        "password", "api_key", "token", "secret", "cookie", "authorization", 
        "credential", "smtp", "app_password", "key"
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


def get_operations_snapshot() -> dict:
    """Generate a read-only snapshot of HuntFlow operations health, logs, alerts, and queue status."""
    now = timezone.now()
    
    # 1. Daemon Status and actual process status
    status, _ = DaemonStatus.objects.get_or_create(pk=1)
    process_state = is_pid_alive(status.pid)
    
    # Check running actual based on process state and heartbeat
    is_running_actual = False
    if process_state == "running":
        if status.last_heartbeat and (now - status.last_heartbeat).total_seconds() < 600:
            is_running_actual = True
            
    heartbeat_age_seconds = None
    if status.last_heartbeat:
        heartbeat_age_seconds = (now - status.last_heartbeat).total_seconds()
        
    daemon_health = {
        "is_running": status.is_running,
        "is_running_actual": is_running_actual,
        "process_state": process_state,
        "pid": status.pid,
        "last_heartbeat": status.last_heartbeat,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "last_error": status.last_error,
        "active_profile_username": status.active_profile_username,
    }
    
    # 2. Launch readiness checklist
    readiness = check_launch_readiness()
    
    # 3. Latest live LaunchRun
    latest_run = LaunchRun.objects.filter(mode="live").first()
    latest_run_data = None
    if latest_run:
        latest_run_data = {
            "id": latest_run.id,
            "status": latest_run.status,
            "started_at": latest_run.started_at,
            "ended_at": latest_run.ended_at,
            "operator": latest_run.operator.username if latest_run.operator else "System",
            "stop_reason": latest_run.stop_reason,
        }
        
    # 4. Running tasks
    running_tasks_qs = Task.objects.filter(status=Task.Status.RUNNING).order_by("-started_at")
    running_tasks = []
    for t in running_tasks_qs:
        task_camp_id = t.payload.get("campaign_id")
        camp = Campaign.objects.filter(id=task_camp_id).first()
        running_tasks.append({
            "id": t.id,
            "task_type": t.task_type,
            "started_at": t.started_at,
            "campaign_name": camp.name if camp else "Unknown",
            "payload": mask_sensitive_payload_recursive(t.payload),
        })
        
    # 5. Next executable task (safe read-only)
    next_task_obj = Task.objects.pending_executable().filter(scheduled_at__lte=now).order_by("scheduled_at").first()
    next_task = None
    if next_task_obj:
        task_camp_id = next_task_obj.payload.get("campaign_id")
        camp = Campaign.objects.filter(id=task_camp_id).first()
        next_task = {
            "id": next_task_obj.id,
            "task_type": next_task_obj.task_type,
            "scheduled_at": next_task_obj.scheduled_at,
            "campaign_name": camp.name if camp else "Unknown",
            "payload": mask_sensitive_payload_recursive(next_task_obj.payload),
        }
        
    # 6. Task counts & Breakdown
    pending_executable_count = Task.objects.pending_executable().count()
    overdue_count = Task.objects.pending_executable().filter(scheduled_at__lte=now).count()
    failed_count = Task.objects.filter(status=Task.Status.FAILED).count()
    cancelled_count = Task.objects.filter(status=Task.Status.CANCELLED).count()

    from openoutreach.career.models import Job, JobMatchAssessment, CandidateApplicationProfile
    queue_breakdown = {
        "executable_now": Task.objects.pending_executable().filter(scheduled_at__lte=now).count(),
        "future_eligible": Task.objects.pending_executable().filter(scheduled_at__gt=now).count(),
        "waiting_linkedin_checkpoint": Task.objects.filter(task_type__in=[Task.TaskType.CONNECT, Task.TaskType.FOLLOW_UP, Task.TaskType.CHECK_PENDING], status=Task.Status.PENDING).count() if LinkedInProfile.objects.filter(id=1, session_status="checkpoint").exists() else 0,
        "waiting_gmail_connection": 0,
        "quarantined_historical": Task.objects.filter(status=Task.Status.FAILED).count(),
        "jobs_discovered_today": Job.objects.filter(created_at__gte=now.replace(hour=0, minute=0, second=0)).count(),
        "jobs_matched_today": JobMatchAssessment.objects.filter(created_at__gte=now.replace(hour=0, minute=0, second=0)).count(),
        "applications_prepared": CandidateApplicationProfile.objects.filter(confirmed=True).count(),
    }
    
    # 7. Recent activity (last 5, masked)
    recent_completed_qs = Task.objects.filter(status=Task.Status.COMPLETED).order_by("-completed_at")[:5]
    recent_completed_tasks = []
    for t in recent_completed_qs:
        recent_completed_tasks.append({
            "id": t.id,
            "task_type": t.task_type,
            "completed_at": t.completed_at,
            "payload": mask_sensitive_payload_recursive(t.payload),
        })
        
    recent_failed_qs = Task.objects.filter(status=Task.Status.FAILED).order_by("-completed_at")[:5]
    if not recent_failed_qs.exists():
        recent_failed_qs = Task.objects.filter(status=Task.Status.FAILED).order_by("-id")[:5]
    recent_failed_tasks = []
    for t in recent_failed_qs:
        recent_failed_tasks.append({
            "id": t.id,
            "task_type": t.task_type,
            "completed_at": t.completed_at or t.created_at,
            "payload": mask_sensitive_payload_recursive(t.payload),
        })
        
    recent_action_logs_qs = ActionLog.objects.select_related("linkedin_profile", "campaign").order_by("-created_at")[:5]
    recent_action_logs = []
    for log in recent_action_logs_qs:
        recent_action_logs.append({
            "id": log.id,
            "action_type": log.action_type,
            "linkedin_profile": log.linkedin_profile.linkedin_username,
            "campaign_name": log.campaign.name,
            "created_at": log.created_at,
            "metadata": mask_sensitive_payload_recursive(log.metadata or {}),
        })
        
    recent_lead_events_qs = LeadPipelineEvent.objects.select_related("lead", "operator").order_by("-created_at")[:5]
    recent_lead_events = []
    for log in recent_lead_events_qs:
        recent_lead_events.append({
            "id": log.id,
            "lead_identifier": log.lead.public_identifier,
            "action": log.action,
            "previous_state": log.previous_state,
            "new_state": log.new_state,
            "operator": log.operator.username if log.operator else "System",
            "note": log.note,
            "created_at": log.created_at,
            "metadata": mask_sensitive_payload_recursive(log.metadata or {}),
        })
        
    recent_queue_events_qs = TaskQueueEvent.objects.select_related("operator", "campaign").order_by("-created_at")[:5]
    recent_queue_events = []
    for log in recent_queue_events_qs:
        recent_queue_events.append({
            "id": log.id,
            "task_id": log.task_id_snapshot,
            "action": log.action,
            "operator": log.operator.username if log.operator else "System",
            "previous_status": log.previous_status,
            "new_status": log.new_status,
            "note": log.note,
            "created_at": log.created_at,
            "metadata": mask_sensitive_payload_recursive(log.metadata or {}),
        })
        
    recent_launch_events_qs = LaunchRunEvent.objects.select_related("operator", "launch_run").order_by("-created_at")[:5]
    recent_launch_events = []
    for log in recent_launch_events_qs:
        recent_launch_events.append({
            "id": log.id,
            "launch_run_id": log.launch_run_id,
            "event_type": log.event_type,
            "operator": log.operator.username if log.operator else "System",
            "note": log.note,
            "created_at": log.created_at,
            "metadata": mask_sensitive_payload_recursive(log.metadata or {}),
        })
        
    # 8. Campaigns
    active_campaigns = list(Campaign.objects.filter(is_active=True).values("id", "name"))
    
    # Paused campaigns with ignored pending work
    paused_campaigns_qs = Campaign.objects.filter(is_active=False)
    paused_campaigns_with_ignored_pending = []
    for camp in paused_campaigns_qs:
        ignored_count = Task.objects.filter(status=Task.Status.PENDING).filter(payload__campaign_id=camp.id).count()
        if ignored_count > 0:
            paused_campaigns_with_ignored_pending.append({
                "id": camp.id,
                "name": camp.name,
                "ignored_count": ignored_count,
            })
            
    # 9. Compilation of alerts
    alerts = []
    
    # - Hard Alerts
    # Daemon marked running but last heartbeat is older than 10 minutes (600s)
    if status.is_running and heartbeat_age_seconds and heartbeat_age_seconds > 600:
        alerts.append({
            "severity": "hard",
            "title": "Daemon heartbeat stale",
            "description": f"The daemon process is marked active, but its last heartbeat was {int(heartbeat_age_seconds // 60)} minutes ago.",
            "fix_link": "/dashboard/daemon/",
        })
        
    # A running task has been active for more than 30 minutes
    stuck_task = Task.objects.filter(status=Task.Status.RUNNING, started_at__lt=now - timedelta(minutes=30)).first()
    if stuck_task:
        alerts.append({
            "severity": "hard",
            "title": "Running task stuck",
            "description": f"Task #{stuck_task.id} ({stuck_task.task_type}) has been running since {stuck_task.started_at} (more than 30 minutes).",
            "fix_link": "/dashboard/tasks/",
        })
        
    # Hard launch blockers exist in readiness summary
    if readiness["overall_status"] == "blocked":
        alerts.append({
            "severity": "hard",
            "title": "Hard launch blockers exist",
            "description": "One or more launch readiness checks are blocking execution. Fix LLM, LinkedInProfile, or campaign issues.",
            "fix_link": "/dashboard/readiness/",
        })
        
    # Daemon is running, but there are no pending executable tasks in the queue
    if status.is_running and pending_executable_count == 0:
        alerts.append({
            "severity": "hard",
            "title": "No executable tasks in queue",
            "description": "The daemon is active, but the queue has no pending executable slots. Outreach is idle.",
            "fix_link": "/dashboard/tasks/",
        })
        
    # - Warnings
    # FAILED tasks exist in the queue
    if failed_count > 0:
        alerts.append({
            "severity": "warning",
            "title": "Failed tasks present",
            "description": f"There are {failed_count} failed tasks in the queue. Operator review is recommended.",
            "fix_link": "/dashboard/tasks/?failed_only=yes",
        })
        
    # More than 10 overdue tasks exist
    if overdue_count > 10:
        alerts.append({
            "severity": "warning",
            "title": "High overdue task count",
            "description": f"There are {overdue_count} tasks ready now but overdue. The scheduler is lagging.",
            "fix_link": "/dashboard/tasks/?overdue_only=yes",
        })
        
    # Paused campaigns have ignored pending tasks
    if paused_campaigns_with_ignored_pending:
        total_ignored = sum(c["ignored_count"] for c in paused_campaigns_with_ignored_pending)
        alerts.append({
            "severity": "warning",
            "title": "Paused campaign tasks ignored",
            "description": f"There are {total_ignored} pending tasks mapped to paused campaigns. They will not execute.",
            "fix_link": "/dashboard/campaigns/",
        })
        
    # BetterContact is unconfigured and there are deals in READY_TO_EMAIL
    cfg = SiteConfig.load()
    if not cfg.bettercontact_api_key:
        ready_emails_count = Deal.objects.filter(state=DealState.READY_TO_EMAIL).count()
        if ready_emails_count > 0:
            alerts.append({
                "severity": "warning",
                "title": "BetterContact unconfigured",
                "description": f"BetterContact lookup is missing while {ready_emails_count} deal(s) are waiting to send emails.",
                "fix_link": "/dashboard/settings/bettercontact/",
            })
            
    # The latest launch run failed recently
    if latest_run and latest_run.status == "failed":
        alerts.append({
            "severity": "warning",
            "title": "Latest launch run failed",
            "description": f"Launch Run #{latest_run.id} terminated with a failed status at {latest_run.ended_at}.",
            "fix_link": f"/dashboard/launch/history/{latest_run.id}/",
        })
        
    # CANCELLED tasks exist
    if cancelled_count > 0:
        alerts.append({
            "severity": "warning",
            "title": "Cancelled tasks exist",
            "description": f"There are {cancelled_count} cancelled tasks in the queue.",
            "fix_link": "/dashboard/tasks/",
        })

    # 10. Next recommended operator action
    if alerts:
        hards = [a for a in alerts if a["severity"] == "hard"]
        warns = [a for a in alerts if a["severity"] == "warning"]
        if hards:
            next_action = {
                "title": f"Resolve hard alert: {hards[0]['title']}",
                "description": hards[0]["description"],
                "fix_link": hards[0]["fix_link"],
            }
        else:
            next_action = {
                "title": f"Resolve warning: {warns[0]['title']}",
                "description": warns[0]["description"],
                "fix_link": warns[0]["fix_link"],
            }
    else:
        next_action = {
            "title": "Monitor runtime state",
            "description": "No immediate alerts active. Monitor logs and upcoming previews.",
            "fix_link": "/dashboard/launch/preview/",
        }
        
    return {
        "daemon_health": daemon_health,
        "readiness_summary": readiness,
        "latest_launch_run": latest_run_data,
        "running_tasks": running_tasks,
        "next_task": next_task,
        "pending_executable_count": pending_executable_count,
        "overdue_count": overdue_count,
        "failed_count": failed_count,
        "cancelled_count": cancelled_count,
        "recent_completed_tasks": recent_completed_tasks,
        "recent_failed_tasks": recent_failed_tasks,
        "recent_action_logs": recent_action_logs,
        "recent_lead_events": recent_lead_events,
        "recent_queue_events": recent_queue_events,
        "recent_launch_events": recent_launch_events,
        "active_campaigns": active_campaigns,
        "paused_campaigns_with_ignored_pending": paused_campaigns_with_ignored_pending,
        "queue_breakdown": queue_breakdown,
        "alerts": alerts,
        "next_recommended_action": next_action,
    }
