# openoutreach/core/readiness.py
from __future__ import annotations

import os
import sys
import errno
from datetime import timedelta
from django.utils import timezone
from django.db.models import Q
from openoutreach.core.models import SiteConfig, Task, Campaign, DaemonStatus, TaskQueueEvent
from openoutreach.crm.models import Lead, Deal, DealState, LeadPipelineEvent
from openoutreach.linkedin.models import LinkedInProfile, ActionLog


def is_pid_alive(pid: int) -> str:
    """Safe three-state daemon process check.
    
    Returns:
      - 'running': Process is verified as active on the system and matches HuntFlow daemon identity.
      - 'stopped': Process is verified as dead/non-existent.
      - 'unknown': Query raised an exception, permission denied, or identity cannot be verified.
    """
    if not pid:
        return "stopped"
    try:
        # os.kill(pid, 0) checks process existence on both Unix and Windows
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return "stopped"
        elif e.errno == errno.EPERM:
            return "unknown"  # Active but lack signal privileges
        else:
            return "unknown"
    except Exception:
        return "unknown"

    try:
        import subprocess
        from django.conf import settings

        def get_normalized_path(p: str) -> str:
            return os.path.abspath(os.path.normpath(p)).replace("\\", "/").lower()

        base_dir_norm = get_normalized_path(str(settings.BASE_DIR))
        sys_exe_norm = get_normalized_path(sys.executable)

        if os.name != "nt":
            res = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if res.returncode != 0:
                return "unknown"
            cmd = res.stdout.strip()
            if not cmd:
                return "unknown"
            
            cmd_norm = cmd.replace("\\", "/").lower()
            if "rundaemon" in cmd_norm and "manage.py" in cmd_norm:
                if base_dir_norm in cmd_norm or sys_exe_norm in cmd_norm:
                    return "running"
            return "unknown"
        else:
            # Layered Windows process identity checks
            # Layer 1: PowerShell Get-CimInstance
            try:
                res = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-Command",
                        f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | Select-Object -ExpandProperty CommandLine",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                if res.returncode == 0:
                    cmd = res.stdout.strip()
                    if cmd:
                        cmd_norm = cmd.replace("\\", "/").lower()
                        if "rundaemon" in cmd_norm and "manage.py" in cmd_norm:
                            if base_dir_norm in cmd_norm or sys_exe_norm in cmd_norm:
                                return "running"
                        return "unknown"
            except Exception:
                pass

            try:
                res = subprocess.run(
                    [
                        "wmic.exe",
                        "process",
                        "where",
                        f"ProcessId={pid}",
                        "get",
                        "CommandLine",
                        "/format:list",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                if res.returncode == 0:
                    cmd = res.stdout.strip()
                    if "CommandLine=" in cmd:
                        cmd_val = cmd.split("CommandLine=", 1)[1].strip()
                        cmd_norm = cmd_val.replace("\\", "/").lower()
                        if "rundaemon" in cmd_norm and "manage.py" in cmd_norm:
                            if base_dir_norm in cmd_norm or sys_exe_norm in cmd_norm:
                                return "running"
                        return "unknown"
            except Exception:
                pass

            return "unknown"
    except Exception:
        return "unknown"


def get_daemon_liveness_classification(status=None, heartbeat_ttl_seconds: int = 45) -> dict:
    """Classify daemon state automatically based on DB status, PID liveness, and heartbeat freshness (10s interval, 45s TTL).

    Classifications:
      - 'HEALTHY': PID is alive and heartbeat is within TTL.
      - 'STALE_PID': DB says running but PID is dead/absent.
      - 'STALE_HEARTBEAT': PID exists but heartbeat is older than TTL.
      - 'STOPPED': DB is_running is False and process is stopped.
    """
    from openoutreach.core.models import DaemonStatus
    if status is None:
        status, _ = DaemonStatus.objects.get_or_create(pk=1)

    os_state = is_pid_alive(status.pid)
    pid_alive = (os_state == "running")

    now = timezone.now()
    heartbeat_fresh = False
    if status.last_heartbeat:
        heartbeat_fresh = (now - status.last_heartbeat).total_seconds() <= heartbeat_ttl_seconds

    if not status.is_running and not pid_alive:
        classification = "STOPPED"
    elif status.is_running and not pid_alive:
        classification = "STALE_PID"
        # Auto-heal DB state
        status.is_running = False
        status.pid = None
        status.save(update_fields=["is_running", "pid"])
    elif pid_alive and not heartbeat_fresh:
        classification = "STALE_HEARTBEAT"
    elif pid_alive and heartbeat_fresh:
        classification = "HEALTHY"
    else:
        classification = "STOPPED"

    return {
        "classification": classification,
        "is_running_db": status.is_running,
        "os_state": os_state,
        "pid": status.pid,
        "last_heartbeat": status.last_heartbeat,
        "heartbeat_fresh": heartbeat_fresh,
    }



def safe_stop_daemon(status) -> dict:
    """Safely stop the daemon process matching status.pid with strict identity validation.
    
    Returns a dict with:
      - success (bool)
      - result (str): "already_stopped", "stopped", "signal_failed", "abort_unknown", "timeout"
      - process_state (str): "stopped", "running", "unknown"
      - error (str, optional)
    """
    pid = status.pid
    if not pid:
        status.is_running = False
        status.save(update_fields=["is_running"])
        return {
            "success": True,
            "result": "already_stopped",
            "process_state": "stopped",
        }

    # 1 & 2. Perform the first identity check
    state1 = is_pid_alive(pid)
    if state1 == "stopped":
        status.is_running = False
        status.pid = None
        status.save(update_fields=["is_running", "pid"])
        return {
            "success": True,
            "result": "already_stopped",
            "process_state": "stopped",
        }
    elif state1 == "unknown":
        return {
            "success": False,
            "result": "abort_unknown",
            "process_state": "unknown",
            "error": f"Process state for PID {pid} is unknown or cannot be verified."
        }

    # 3. Perform a second identity check immediately before signaling
    state2 = is_pid_alive(pid)
    if state2 != "running":
        return {
            "success": False,
            "result": "abort_unknown",
            "process_state": state2,
            "error": f"Process status changed to {state2} immediately before signaling."
        }

    # 4. Platform-specific stop behavior
    try:
        import signal
        if os.name != "nt":
            os.kill(pid, signal.SIGTERM)
        else:
            # Windows termination behavior matching the existing implementation
            os.kill(pid, signal.SIGTERM)
    except Exception as e:
        return {
            "success": False,
            "result": "signal_failed",
            "process_state": "unknown",
            "error": f"Failed to send SIGTERM: {e}"
        }

    # 5. Wait up to 5 seconds, polling every 0.5 seconds
    import time
    timeout = 5.0
    check_interval = 0.5
    elapsed = 0.0
    terminated = False

    while elapsed < timeout:
        time.sleep(check_interval)
        elapsed += check_interval
        current_state = is_pid_alive(pid)
        if current_state == "stopped":
            terminated = True
            break

    # 6. Clear PID only after confirmed stopped
    if terminated:
        status.is_running = False
        status.pid = None
        status.save(update_fields=["is_running", "pid"])
        return {
            "success": True,
            "result": "stopped",
            "process_state": "stopped",
        }
    else:
        # Retain the PID and diagnostic state if termination cannot be confirmed
        return {
            "success": False,
            "result": "timeout",
            "process_state": "unknown",
            "error": f"SIGTERM was sent to PID {pid}, but the process did not stop within 5 seconds."
        }



def check_launch_readiness(campaign_id: int | None = None) -> dict:
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cfg = SiteConfig.load()
    
    # 1. System Config Counts & Verification
    llm_configured = bool(cfg.llm_api_key and cfg.ai_model)
    bettercontact_configured = bool(cfg.bettercontact_api_key)
    
    from openoutreach.emails.models import Mailbox
    has_mailboxes = Mailbox.objects.exists()
    
    # 2. LinkedIn Profiles
    active_profiles = LinkedInProfile.objects.filter(active=True)
    total_profiles_count = LinkedInProfile.objects.count()
    active_profiles_count = active_profiles.count()
    
    profiles_missing_creds = []
    profiles_missing_self_lead = []
    
    for p in active_profiles:
        if not p.linkedin_username or not p.linkedin_password:
            profiles_missing_creds.append(p)
        if not p.self_lead:
            profiles_missing_self_lead.append(p)

    # 3. Campaigns
    campaigns_qs = Campaign.objects.all()
    if campaign_id is not None:
        campaigns_qs = campaigns_qs.filter(id=campaign_id)
        
    total_campaigns_count = campaigns_qs.count()
    active_campaigns = campaigns_qs.filter(is_active=True)
    active_campaigns_count = active_campaigns.count()
    paused_campaigns_count = campaigns_qs.filter(is_active=False).count()
    
    campaigns_no_users = []
    campaigns_no_notes = []
    campaigns_sharing_accounts = []
    
    for c in active_campaigns:
        if not c.users.exists():
            campaigns_no_users.append(c)
        if c.connection_note_enabled and not c.connection_note_template:
            # warning only
            campaigns_no_notes.append(c)
            
    # Detect shared profiles
    all_assigned_users = []
    for c in active_campaigns:
        all_assigned_users.extend(c.users.all())
    user_shares = {}
    for u in all_assigned_users:
        user_shares[u.id] = user_shares.get(u.id, 0) + 1
    shared_users_count = sum(1 for uid, cnt in user_shares.items() if cnt > 1)

    # 4. Lead Pipeline
    deals_qs = Deal.objects.all()
    leads_qs = Lead.objects.all()
    if campaign_id is not None:
        deals_qs = deals_qs.filter(campaign_id=campaign_id)
        leads_qs = leads_qs.filter(deal__campaign_id=campaign_id)
        
    discovered_leads = leads_qs.distinct().count()
    
    funnel_counts = {}
    for state in DealState:
        funnel_counts[state.value] = deals_qs.filter(state=state).count()
        
    unresolved_emails = deals_qs.filter(state=DealState.QUALIFIED, lead__api_email__isnull=True).count()
    ready_to_email = deals_qs.filter(state=DealState.READY_TO_EMAIL).count()

    # 5. Tasks
    tasks_qs = Task.objects.all()
    
    # Executable pending tasks only (excluding paused campaigns)
    paused_camps = Campaign.objects.filter(is_active=False)
    paused_camp_ids = list(paused_camps.values_list("id", flat=True))
    
    pending_tasks = tasks_qs.filter(status=Task.Status.PENDING)
    failed_tasks = tasks_qs.filter(status=Task.Status.FAILED)
    cancelled_tasks = tasks_qs.filter(status=Task.Status.CANCELLED)
    
    if campaign_id is not None:
        # In-memory filter or JSON query
        pending_executable_tasks = [t for t in pending_tasks if t.payload.get("campaign_id") == campaign_id]
        failed_tasks_count = sum(1 for t in failed_tasks if t.payload.get("campaign_id") == campaign_id)
        cancelled_tasks_count = sum(1 for t in cancelled_tasks if t.payload.get("campaign_id") == campaign_id)
    else:
        pending_executable_tasks = [t for t in pending_tasks if t.payload.get("campaign_id") not in paused_camp_ids]
        failed_tasks_count = failed_tasks.count()
        cancelled_tasks_count = cancelled_tasks.count()

    ignored_tasks_count = 0
    if campaign_id is None:
        ignored_tasks_count = sum(1 for t in pending_tasks if t.payload.get("campaign_id") in paused_camp_ids)

    overdue_tasks = []
    for t in pending_executable_tasks:
        if t.scheduled_at < now:
            overdue_tasks.append(t)
            
    next_task = None
    future_tasks = [t for t in pending_executable_tasks if t.scheduled_at >= now]
    if future_tasks:
        future_tasks.sort(key=lambda x: x.scheduled_at)
        next_task = future_tasks[0]

    # 6. Daemon Status & Process State
    daemon_status = DaemonStatus.objects.filter(pk=1).first()
    daemon_is_running = daemon_status.is_running if daemon_status else False
    daemon_last_heartbeat = daemon_status.last_heartbeat if daemon_status else None
    daemon_pid = daemon_status.pid if daemon_status else None
    
    daemon_process_state = "stopped"
    if daemon_is_running and daemon_pid:
        daemon_process_state = is_pid_alive(daemon_pid)
        
    heartbeat_age_seconds = 0
    stale_heartbeat = False
    if daemon_last_heartbeat:
        heartbeat_age_seconds = (now - daemon_last_heartbeat).total_seconds()
        if heartbeat_age_seconds > 600:  # 10 minutes
            stale_heartbeat = True

    # 7. Collect Hard Blockers
    hard_blockers = []
    
    if not llm_configured:
        hard_blockers.append({
            "title": "LLM not configured",
            "description": "An LLM API key and model prefix are required to generate notes and qualify profiles.",
            "fix_link": "/dashboard/settings/llm/"
        })
        
    if active_profiles_count == 0:
        hard_blockers.append({
            "title": "No active LinkedIn account",
            "description": "At least one active LinkedIn account is required to perform search scrapes and connection requests.",
            "fix_link": "/dashboard/linkedin-accounts/"
        })
    elif profiles_missing_creds:
        hard_blockers.append({
            "title": "Active LinkedIn profile missing credentials",
            "description": "One or more active LinkedIn profiles are missing their username or password credentials.",
            "fix_link": "/dashboard/linkedin-accounts/"
        })
        
    if active_campaigns_count == 0:
        hard_blockers.append({
            "title": "No active campaigns",
            "description": "At least one campaign must be active to schedule and execute outreach tasks.",
            "fix_link": "/dashboard/campaigns/"
        })
        
    if len(pending_executable_tasks) == 0:
        hard_blockers.append({
            "title": "No executable pending tasks",
            "description": "The task queue has no pending slots scheduled for execution in active campaigns. Rebuild the queue.",
            "fix_link": "/dashboard/campaigns/"
        })

    # Check connect and follow-up tasks for missing/ambiguous recipients
    active_camp_ids = list(active_campaigns.values_list("id", flat=True))
    pending_cf_tasks = Task.objects.filter(
        status=Task.Status.PENDING,
        task_type__in=["connect", "follow_up"]
    )
    if campaign_id is not None:
        pending_cf_tasks = [t for t in pending_cf_tasks if t.payload.get("campaign_id") == campaign_id]
    else:
        pending_cf_tasks = [t for t in pending_cf_tasks if t.payload.get("campaign_id") in active_camp_ids]

    # Only check tasks that are due/overdue (scheduled_at <= now)
    pending_cf_tasks = [t for t in pending_cf_tasks if t.scheduled_at <= now]

    missing_recipient_tasks = []
    for task in pending_cf_tasks:
        task_camp_id = task.payload.get("campaign_id")
        camp = Campaign.objects.filter(id=task_camp_id).first()
        if not camp:
            missing_recipient_tasks.append(task.id)
            continue
            
        lead_id = task.payload.get("lead_id") or task.payload.get("lead_public_id") or task.payload.get("public_id")
        if lead_id:
            if isinstance(lead_id, int):
                lead_exists = Lead.objects.filter(id=lead_id).exists()
            else:
                lead_exists = Lead.objects.filter(public_identifier=lead_id).exists()
            if not lead_exists:
                missing_recipient_tasks.append(task.id)
        else:
            if task.task_type == "connect":
                ready_deals = Deal.objects.filter(campaign=camp, state=DealState.READY_TO_CONNECT)
                if ready_deals.count() != 1:
                    missing_recipient_tasks.append(task.id)
            elif task.task_type == "follow_up":
                from openoutreach.linkedin.tasks.follow_up import _connected_deals, _too_soon_to_nudge
                connected_deals = _connected_deals(camp)
                eligible_deals = [d for d in connected_deals if not _too_soon_to_nudge(d)]
                if len(eligible_deals) != 1:
                    missing_recipient_tasks.append(task.id)

    if missing_recipient_tasks:
        hard_blockers.append({
            "title": "Missing or ambiguous outreach recipients",
            "description": f"The following connect or follow-up task(s) do not have exactly one resolved recipient: {', '.join(map(str, missing_recipient_tasks))}.",
            "fix_link": "/dashboard/tasks/"
        })

    # 8. Collect Warnings
    warnings = []
    
    if failed_tasks_count > 0:
        warnings.append({
            "title": "Failed tasks present in active campaigns",
            "description": f"There are {failed_tasks_count} failed tasks in the queue. Review and retry them to recover outreach slots.",
            "fix_link": "/dashboard/tasks/?failed_only=yes"
        })
        
    if len(overdue_tasks) > 5:
        warnings.append({
            "title": "Many overdue tasks",
            "description": f"There are {len(overdue_tasks)} pending tasks overdue. The daemon might be stopped or running behind schedule.",
            "fix_link": "/dashboard/tasks/?overdue_only=yes"
        })
        
    if ignored_tasks_count > 0:
        warnings.append({
            "title": "Paused campaigns with ignored pending tasks",
            "description": f"There are {ignored_tasks_count} pending tasks left in the queue for paused campaigns. Consider clearing their pending queues.",
            "fix_link": "/dashboard/campaigns/"
        })
        
    if has_mailboxes and not bettercontact_configured:
        # Check if unresolved emails or ready to email deals exist
        if unresolved_emails > 0 or ready_to_email > 0:
            warnings.append({
                "title": "BetterContact missing with unresolved leads or email-ready deals",
                "description": "Mailboxes are configured for email outreach, but BetterContact key is missing while email-dependent deals exist.",
                "fix_link": "/dashboard/settings/bettercontact/"
            })
            
    if campaigns_no_notes:
        warnings.append({
            "title": "Campaigns missing connection-note template",
            "description": f"{len(campaigns_no_notes)} active campaign(s) have notes enabled but are using the blank template fallback.",
            "fix_link": "/dashboard/campaigns/"
        })
        
    if profiles_missing_self_lead:
        warnings.append({
            "title": "No self-profile linked",
            "description": f"{len(profiles_missing_self_lead)} LinkedIn profile(s) have not linked their own profile lead. Non-fatal.",
            "fix_link": "/dashboard/linkedin-accounts/"
        })
        
    if daemon_is_running and stale_heartbeat:
        warnings.append({
            "title": "Stale daemon heartbeat",
            "description": f"The daemon is marked running, but its last heartbeat was {int(heartbeat_age_seconds // 60)} minutes ago.",
            "fix_link": "/dashboard/daemon/"
        })
        
    if daemon_is_running and daemon_process_state == "running":
        warnings.append({
            "title": "Duplicate process risk",
            "description": "A running daemon process was detected on the host machine. Ensure you do not start a duplicate instance.",
            "fix_link": "/dashboard/daemon/"
        })
        
    if daemon_process_state == "unknown":
        warnings.append({
            "title": "Daemon process status unknown",
            "description": "Could not verify process status on the system due to security permissions or platform limits.",
            "fix_link": "/dashboard/daemon/"
        })

    # Overall status
    if hard_blockers:
        overall_status = "blocked"
    elif warnings:
        overall_status = "warning"
    else:
        overall_status = "ready"

    # Next recommended action logic
    if not llm_configured:
        next_action_title = "Configure LLM credentials"
        next_action_description = "Add your API key and model prefix in LLM settings to enable profile parsing and note generation."
        next_action_link = "/dashboard/settings/llm/"
    elif active_profiles_count == 0:
        next_action_title = "Add or activate a LinkedIn account"
        next_action_description = "Create or activate at least one LinkedIn account to launch search queries and connection note runs."
        next_action_link = "/dashboard/linkedin-accounts/"
    elif active_campaigns_count == 0:
        next_action_title = "Activate at least one campaign"
        next_action_description = "Enable at least one campaign from the campaign list page to begin slot allocation planning."
        next_action_link = "/dashboard/campaigns/"
    elif len(pending_executable_tasks) == 0:
        next_action_title = "Reconcile campaign queue"
        next_action_description = "No pending slots exist in active campaigns. Click Reconcile on the Campaign list page to plan slots."
        next_action_link = "/dashboard/campaigns/"
    elif failed_tasks_count > 0:
        next_action_title = "Review failed tasks"
        next_action_description = "Review and retry failed slots in the task queue to salvage outreach flows."
        next_action_link = "/dashboard/tasks/?failed_only=yes"
    elif daemon_process_state == "stopped" or stale_heartbeat:
        next_action_title = "Start the HuntFlow daemon"
        next_action_description = "All checks are good. Launch the background daemon process to execute outreach tasks."
        next_action_link = "/dashboard/daemon/"
    else:
        next_action_title = "Outreach daemon is running"
        next_action_description = "The background daemon is running. Monitor task logs and active queues in real-time."
        next_action_link = "/dashboard/daemon/"

    next_action = {
        "title": next_action_title,
        "description": next_action_description,
        "fix_link": next_action_link
    }

    # Build sections dict
    sections = {
        "system_config": {
            "llm_configured": llm_configured,
            "ai_model": cfg.ai_model or "Not configured",
            "bettercontact_configured": bettercontact_configured,
            "has_mailboxes": has_mailboxes,
        },
        "linkedin_accounts": {
            "total_profiles": total_profiles_count,
            "active_profiles": active_profiles_count,
            "missing_creds": len(profiles_missing_creds),
            "missing_self_lead": len(profiles_missing_self_lead),
        },
        "campaigns": {
            "total_campaigns": total_campaigns_count,
            "active_campaigns": active_campaigns_count,
            "paused_campaigns": paused_campaigns_count,
            "no_users": len(campaigns_no_users),
            "no_notes": len(campaigns_no_notes),
            "shared_users": shared_users_count,
        },
        "lead_pipeline": {
            "discovered_leads": discovered_leads,
            "qualified": funnel_counts.get("qualified", 0),
            "ready_to_connect": funnel_counts.get("ready_to_connect", 0),
            "pending": funnel_counts.get("pending", 0),
            "connected": funnel_counts.get("connected", 0),
            "completed": funnel_counts.get("completed", 0),
            "failed": funnel_counts.get("failed", 0),
            "unresolved_emails": unresolved_emails,
            "ready_to_email": ready_to_email,
        },
        "task_queue": {
            "pending_executable": len(pending_executable_tasks),
            "ignored_pending": ignored_tasks_count,
            "failed_tasks": failed_tasks_count,
            "cancelled_tasks": cancelled_tasks_count,
            "overdue_tasks": len(overdue_tasks),
            "next_scheduled_task": str(next_task.scheduled_at) if next_task else "None",
        },
        "daemon": {
            "is_running": daemon_is_running,
            "last_heartbeat": str(daemon_last_heartbeat) if daemon_last_heartbeat else "None",
            "pid": daemon_pid,
            "process_state": daemon_process_state,
            "stale_heartbeat": stale_heartbeat,
        }
    }

    return {
        "overall_status": overall_status,
        "hard_blockers": hard_blockers,
        "warnings": warnings,
        "sections": sections,
        "next_action": next_action,
        "daemon_process_state": daemon_process_state,
        "campaign_scoped": campaign_id is not None,
        "scoped_campaign_id": campaign_id,
    }
