# openoutreach/core/daemon.py
from __future__ import annotations

import logging
import random
import sys
import time
from datetime import timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from django.utils import timezone
from pydantic_ai.exceptions import ModelHTTPError

from termcolor import colored

from openoutreach.core.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    CAMPAIGN_CONFIG,
    ENABLE_ACTIVE_HOURS,
)
from openoutreach.linkedin.diagnostics import failure_diagnostics
from linkedin_cli.exceptions import AuthenticationError, CheckpointChallengeError
from openoutreach.linkedin.ml.qualifier import BayesianQualifier
from openoutreach.core.models import Task
from openoutreach.emails.tasks.send import handle_email
from openoutreach.linkedin.tasks.check_pending import handle_check_pending
from openoutreach.linkedin.tasks.connect import handle_connect
from openoutreach.linkedin.tasks.follow_up import handle_follow_up

logger = logging.getLogger(__name__)

def handle_health_check(task, session, qualifiers):
    logger.info("Executing safe internal health check task #%s", task.pk)
    task.mark_completed()


def handle_job_application_task(task, session, qualifiers):
    logger.info("Executing job application task #%s", task.pk)
    from openoutreach.career.services.job_application_engine import execute_job_application
    from openoutreach.career.models import CandidateProfile, Job, ApplicationDraft
    from openoutreach.core.models import Workspace

    candidate_id = task.payload.get("candidate_id")
    job_id = task.payload.get("job_id")
    draft_id = task.payload.get("draft_id")

    candidate = CandidateProfile.objects.filter(pk=candidate_id).first()
    job = Job.objects.filter(pk=job_id).first()
    draft = ApplicationDraft.objects.filter(pk=draft_id).first()
    
    workspace = getattr(draft, "workspace", None) or getattr(job, "workspace", None)
    if not workspace and candidate and candidate.user:
        from openoutreach.core.models import WorkspaceMembership
        mem = WorkspaceMembership.objects.filter(user=candidate.user, status="active").first()
        if mem:
            workspace = mem.workspace
    if not workspace:
        workspace = Workspace.objects.filter(slug="default").first() or Workspace.objects.first()

    if not candidate or not job or not draft:
        task.status = Task.Status.FAILED
        task.payload["reason"] = "missing_job_application_context"
        task.save(update_fields=["status", "payload"])
        return

    if workspace:
        from openoutreach.core.models import WorkspaceAutomationSettings
        auto_settings = WorkspaceAutomationSettings.objects.filter(workspace=workspace).first()
        if auto_settings and not auto_settings.master_automation_enabled and not task.payload.get("manual_trigger") and not task.payload.get("mock_boundary") and not task.payload.get("external_action_authorized"):
            task.status = Task.Status.FAILED
            task.payload["reason"] = "workspace_automation_paused"
            task.save(update_fields=["status", "payload"])
            return

    # Check QA synthetic transport guard (must obey 5 safety conditions)
    from openoutreach.linkedin.transport_guard import is_qa_synthetic_transport_allowed
    is_qa_transport = is_qa_synthetic_transport_allowed(
        workspace=workspace,
        linkedin_profile=getattr(session, "linkedin_profile", None),
        task=task,
    )
    if is_qa_transport and (task.payload.get("simulated") or task.payload.get("mock_boundary")):
        class MockCanaryAdapter:
            def apply(self, **kwargs):
                return {"success": True, "externally_verified": False, "confirmation": "mock_canary_verified"}
        adapter = MockCanaryAdapter()
    else:
        from openoutreach.linkedin.browser.easy_apply import LinkedInEasyApplyAdapter
        adapter = LinkedInEasyApplyAdapter(session=session)

    if workspace:
        from openoutreach.core.models import WorkspaceAutomationSettings
        auto_st = WorkspaceAutomationSettings.objects.filter(workspace=workspace).first()
        if auto_st:
            auto_st.last_job_application_attempt_at = timezone.now()
            auto_st.save(update_fields=["last_job_application_attempt_at", "updated_at"])

    operator_approval = None
    op_app_id = task.payload.get("operator_approval_id")
    if op_app_id:
        from openoutreach.core.models import OperatorActionApproval
        operator_approval = OperatorActionApproval.objects.filter(pk=op_app_id).first()

    from openoutreach.career.models import IncidentHoldTarget
    from django.contrib.contenttypes.models import ContentType
    if candidate:
        cand_ct = ContentType.objects.get_for_model(CandidateProfile)
        active_cand_hold = IncidentHoldTarget.objects.filter(
            incident_hold__active=True,
            content_type=cand_ct,
            object_id=candidate.id
        ).exists()
        if active_cand_hold:
            task.status = Task.Status.FAILED
            task.payload["reason"] = "incident_hold_active"
            task.save(update_fields=["status", "payload"])
            if operator_approval:
                operator_approval.mark_failed(reason="incident_hold_active")
            return

    from openoutreach.linkedin.models import LinkedInProfile
    session_profile = getattr(session, "linkedin_profile", None) if session else None
    if not isinstance(session_profile, LinkedInProfile):
        session_profile = None
    if not session_profile and candidate and candidate.user:
        session_profile = LinkedInProfile.objects.filter(user=candidate.user).first()

    if session_profile:
        from openoutreach.linkedin.services.account_governor import is_account_safe_for_provider_write
        is_safe, safety_reason = is_account_safe_for_provider_write(
            workspace=workspace,
            linkedin_profile=session_profile,
            action_type="job_application",
            task=task,
            candidate=candidate,
        )
        if not is_safe and not task.payload.get("mock_boundary") and not task.payload.get("simulated"):
            logger.warning("Job application task #%s blocked by account governor: %s", task.pk, safety_reason)
            task.status = Task.Status.FAILED
            task.payload["reason"] = safety_reason
            task.save(update_fields=["status", "payload"])
            if operator_approval:
                operator_approval.mark_failed(reason=safety_reason)
            return

    canary_budget = None
    if task.payload.get("stage_canary") or task.payload.get("resume_stage") == "STAGE_1":
        from openoutreach.core.models import StageCanaryBudget
        prof = getattr(session, "linkedin_profile", None)
        claimed, canary_budget, claim_reason = StageCanaryBudget.claim_canary(
            workspace=workspace,
            linkedin_profile=prof,
            stage=task.payload.get("resume_stage", "STAGE_1"),
            task_id=task.pk,
        )
        if not claimed:
            logger.warning("Stage canary claim rejected for Task #%s: %s", task.pk, claim_reason)
            task.status = Task.Status.FAILED
            task.payload["reason"] = claim_reason
            task.save(update_fields=["status", "payload"])
            if operator_approval:
                operator_approval.mark_failed(reason=claim_reason)
            return

    if canary_budget and not (is_qa_transport and (task.payload.get("simulated") or task.payload.get("mock_boundary"))):
        canary_budget.record_network_attempt(task_id=task.pk)

    res = execute_job_application(
        workspace=workspace,
        candidate=candidate,
        job=job,
        draft=draft,
        adapter=adapter,
        external_action_authorized=task.payload.get("external_action_authorized", True),
        operator_approval=operator_approval,
        linkedin_profile=session_profile,
    )

    if canary_budget:
        canary_budget.finalize_canary(res.get("status", "failed"), res.get("reason", ""))

    from openoutreach.career.services.job_application_engine import _safe_db_call

    if res.get("status") == "submitted" and res.get("externally_verified", True):
        def _set_completed():
            task.status = Task.Status.COMPLETED
            task.completed_at = timezone.now()
            task.save(update_fields=["status", "completed_at"])
        _safe_db_call(_set_completed)
    elif res.get("status") == "submission_unverified":
        def _set_unverified():
            task.status = Task.Status.COMPLETED
            task.completed_at = timezone.now()
            task.payload["unverified"] = True
            task.save(update_fields=["status", "completed_at", "payload"])
        _safe_db_call(_set_unverified)
    elif res.get("status") == "needs_your_answer" or res.get("reason") == "unknown_screening_questions":
        def _set_needs_answer():
            task.status = Task.Status.FAILED
            task.payload["reason"] = "needs_your_answer"
            task.payload["unknown_questions"] = res.get("unknown_questions", [])
            task.save(update_fields=["status", "payload"])
        _safe_db_call(_set_needs_answer)
    elif res.get("status") == "submission_uncertain" or res.get("uncertainty"):
        def _set_uncertain_stop():
            task.status = Task.Status.FAILED
            task.payload["reason"] = "provider_submission_uncertainty"
            task.save(update_fields=["status", "payload"])
            if workspace:
                from openoutreach.core.models import WorkspaceAutomationSettings
                auto_st = WorkspaceAutomationSettings.objects.filter(workspace=workspace).first()
                if auto_st:
                    auto_st.trigger_safety_stop("provider_submission_uncertainty")
        _safe_db_call(_set_uncertain_stop)
    else:
        def _set_failed():
            task.status = Task.Status.FAILED
            task.payload["reason"] = res.get("reason", "submission_failed")
            task.save(update_fields=["status", "payload"])
        _safe_db_call(_set_failed)


def handle_job_discovery_task(task, session, qualifiers):
    logger.info("Executing autonomous job discovery task #%s", task.pk)
    from openoutreach.career.services.job_discovery_service import discover_and_evaluate_jobs
    from openoutreach.career.models import CandidateProfile
    from openoutreach.core.models import Workspace, WorkspaceAutomationSettings
    from django.utils import timezone

    workspace_id = task.payload.get("workspace_id")
    candidate_id = task.payload.get("candidate_id")

    workspace = None
    if workspace_id:
        workspace = Workspace.objects.filter(pk=workspace_id).first()
    if not workspace:
        workspace = Workspace.objects.filter(slug="default").first()

    candidate = None
    if candidate_id:
        candidate = CandidateProfile.objects.filter(pk=candidate_id).first()
    elif session and getattr(session, "django_user", None):
        from django.contrib.auth.models import User
        if isinstance(session.django_user, User):
            candidate = CandidateProfile.objects.filter(user=session.django_user).first()
    if not candidate and workspace:
        candidate = CandidateProfile.objects.filter(user__workspace_memberships__workspace=workspace).first()
        if not candidate:
            candidate = CandidateProfile.objects.filter(user__campaigns__workspace=workspace).first()

    if not workspace or not candidate:
        logger.error("Job discovery task #%s failed: missing workspace or candidate", task.pk)
        task.mark_failed(reason="missing_workspace_or_candidate")
        return

    # Validate tenancy: candidate must belong to workspace
    from django.db.models import Q
    is_member = CandidateProfile.objects.filter(
        pk=candidate.pk
    ).filter(
        Q(user__workspace_memberships__workspace=workspace) |
        Q(user__campaigns__workspace=workspace) |
        Q(user__is_staff=True)
    ).exists()
    if not is_member:
        logger.error("Candidate #%s does not belong to workspace #%s. Failing closed.", candidate.pk, workspace.pk)
        task.mark_failed(reason="candidate_workspace_mismatch")
        return

    auto_settings = WorkspaceAutomationSettings.get_for_workspace(workspace)
    if not auto_settings or not auto_settings.master_automation_enabled or not auto_settings.auto_apply_jobs:
        logger.info("Job automation is paused in workspace settings. Skipping discovery.")
        task.mark_failed(reason="job_automation_disabled")
        return

    # Check daily limits before discovering
    from openoutreach.core.services.quota_service import (
        get_daily_successful_easy_apply_count,
        get_daily_job_provider_attempt_count,
    )
    successful_today = get_daily_successful_easy_apply_count(workspace=workspace, candidate_profile=candidate)
    attempts_today = get_daily_job_provider_attempt_count(workspace=workspace, candidate_profile=candidate)
    daily_target = getattr(auto_settings, "daily_job_application_target", 50) or 50
    daily_ceiling = auto_settings.max_daily_job_applications or 60

    if successful_today >= daily_target:
        logger.info("Daily job target reached (%d >= %d). Halting discovery.", successful_today, daily_target)
        task.mark_completed()
        return

    if attempts_today >= daily_ceiling:
        logger.info("Daily job attempt ceiling reached (%d >= %d). Halting discovery.", attempts_today, daily_ceiling)
        task.mark_completed()
        return

    now = timezone.now()
    auto_settings.last_job_discovery_started_at = now
    auto_settings.save(update_fields=["last_job_discovery_started_at", "updated_at"])

    try:
        res = discover_and_evaluate_jobs(
            session=session,
            workspace=workspace,
            candidate=candidate,
            max_inspect_per_run=25,
            min_match_threshold=70,
        )
        auto_settings.last_job_discovery_completed_at = timezone.now()
        auto_settings.jobs_inspected_last_cycle = res.get("inspected_count", 0)
        auto_settings.safe_jobs_last_cycle = res.get("enqueued_count", 0)
        auto_settings.last_job_scheduler_error = ""
        auto_settings.save(update_fields=[
            "last_job_discovery_completed_at",
            "jobs_inspected_last_cycle",
            "safe_jobs_last_cycle",
            "last_job_scheduler_error",
            "updated_at",
        ])
        task.payload["result"] = res
        task.mark_completed()
    except Exception as e:
        logger.exception("Job discovery task #%s failed: %s", task.pk, e)
        auto_settings.last_job_scheduler_error = str(e)
        auto_settings.save(update_fields=["last_job_scheduler_error", "updated_at"])
        task.mark_failed(reason=f"job_discovery_error: {str(e)}")


def handle_prospect_discovery_task(task, session, qualifiers):
    logger.info("Executing authoritative prospect discovery task #%s", task.pk)
    from openoutreach.linkedin.services.discovery_service import discover_campaign_targets
    from openoutreach.core.models import Campaign, WorkspaceAutomationSettings
    from django.utils import timezone

    campaign_id = task.payload.get("campaign_id")
    campaign = Campaign.objects.filter(pk=campaign_id).first() if campaign_id else None
    if not campaign:
        task.mark_failed(reason="missing_campaign")
        return

    ws = getattr(campaign, "workspace", None)
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None
    if auto_st and not auto_st.master_automation_enabled and not task.payload.get("manual_trigger") and not task.payload.get("mock_boundary"):
        task.mark_failed(reason="workspace_automation_paused")
        return

    try:
        res = discover_campaign_targets(campaign, limit=10, session=session)
        if auto_st:
            auto_st.last_prospect_discovery_at = timezone.now()
            auto_st.save(update_fields=["last_prospect_discovery_at", "updated_at"])
        task.payload["result"] = res
        task.mark_completed()
    except Exception as e:
        logger.exception("Prospect discovery task #%s failed: %s", task.pk, e)
        task.mark_failed(reason=f"prospect_discovery_error: {str(e)}")


def handle_reply_poll_task(task, session, qualifiers):
    logger.info("Executing inbound reply poll task #%s", task.pk)
    from openoutreach.crm.models import Deal, DealState
    from openoutreach.linkedin.db.chat import sync_conversation
    from openoutreach.core.models import WorkspaceAutomationSettings, Campaign
    from django.utils import timezone

    campaign_id = task.payload.get("campaign_id")
    campaign = Campaign.objects.filter(pk=campaign_id).first() if campaign_id else None
    ws = getattr(campaign, "workspace", None)
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None

    if auto_st and not auto_st.master_automation_enabled and not task.payload.get("manual_trigger") and not task.payload.get("mock_boundary"):
        task.mark_failed(reason="workspace_automation_paused")
        return

    now = timezone.now()
    new_replies_count = 0
    try:
        deals_to_poll = Deal.objects.filter(
            state=DealState.CONNECTED,
        )
        if campaign:
            deals_to_poll = deals_to_poll.filter(campaign=campaign)
        
        for deal in deals_to_poll[:10]:
            if deal.lead and deal.lead.public_identifier:
                try:
                    messages = sync_conversation(session, deal.lead.public_identifier)
                    inbound = [m for m in messages if not m.get("is_outgoing")]
                    if inbound:
                        new_replies_count += 1
                except Exception as e:
                    logger.debug("Failed syncing conversation for %s: %s", deal.lead.public_identifier, e)

        if auto_st:
            auto_st.last_reply_poll_at = now
            auto_st.save(update_fields=["last_reply_poll_at", "updated_at"])

        task.payload["new_replies_count"] = new_replies_count
        task.mark_completed()
    except Exception as e:
        logger.exception("Reply poll task #%s failed: %s", task.pk, e)
        if auto_st:
            auto_st.last_reply_poll_at = now
            auto_st.save(update_fields=["last_reply_poll_at", "updated_at"])
        task.mark_failed(reason=f"reply_poll_error: {str(e)}")


_HANDLERS = {
    Task.TaskType.CONNECT: handle_connect,
    Task.TaskType.CHECK_PENDING: handle_check_pending,
    Task.TaskType.FOLLOW_UP: handle_follow_up,
    Task.TaskType.EMAIL: handle_email,
    Task.TaskType.HEALTH_CHECK: handle_health_check,
    Task.TaskType.JOB_APPLICATION: handle_job_application_task,
    Task.TaskType.JOB_DISCOVERY: handle_job_discovery_task,
    Task.TaskType.PROSPECT_DISCOVERY: handle_prospect_discovery_task,
    Task.TaskType.REPLY_POLL: handle_reply_poll_task,
}

HEARTBEAT_INTERVAL = 300  # 5 minutes
HEARTBEAT_SLICE = 60      # wake every minute during long sleeps


# ── Heartbeat ────────────────────────────────────────────────────────


import threading

class HeartbeatThread(threading.Thread):
    """Dedicated background thread that updates DaemonStatus.last_heartbeat every *interval* seconds.
    
    Operates independently of worker task execution, browser automation, or async Playwright loops.
    Uses pure synchronous ORM calls and closes connections after each tick.
    """
    def __init__(self, interval: float = 10.0):
        super().__init__(name="DaemonHeartbeatThread", daemon=True)
        self.interval = interval
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        from django.db import connection
        from django.utils import timezone
        from openoutreach.core.models import DaemonStatus, WorkspaceAutomationSettings
        while not self._stop_event.is_set():
            try:
                connection.close()
                now = timezone.now()
                DaemonStatus.objects.filter(pk=1).update(
                    last_heartbeat=now,
                    is_running=True,
                )
                WorkspaceAutomationSettings.objects.all().update(
                    last_daemon_heartbeat=now,
                )
            except Exception as e:
                logger.debug("HeartbeatThread update failed: %s", e)
            finally:
                connection.close()
            self._stop_event.wait(self.interval)

class Heartbeat:
    """Legacy compatibility shim wrapping HeartbeatThread if accessed directly."""
    def __init__(self, interval: float = HEARTBEAT_INTERVAL):
        self.interval = interval
    def maybe_log(self, context):
        pass


def _hm(seconds: float) -> str:
    """Format a duration as ``Hh MMm`` (e.g. ``0h08m``)."""
    h, m = int(seconds // 3600), int(seconds % 3600 // 60)
    return f"{h}h{m:02d}m"


def sleep_with_heartbeat(
    seconds: float, heartbeat: Heartbeat, context: str | Callable[[float], str]
) -> None:
    """Sleep that wakes periodically and breaks early if a newly enqueued task is ready."""
    from openoutreach.core.models import Task
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        try:
            next_sec = Task.objects.seconds_to_next()
            if next_sec is not None and next_sec <= 0:
                return
        except Exception:
            pass
        time.sleep(min(2.0, remaining))
        if callable(context):
            heartbeat.maybe_log(lambda: context(max(0.0, end - time.monotonic())))
        else:
            heartbeat.maybe_log(context)


# ── Human-rhythm pacing ──────────────────────────────────────────────


class _HumanRhythmBreak:
    """Wall-clock burst timer that injects a random break between bursts.

    Call ``reset()`` after idle sleeps (active-hours pause, waiting for
    the next scheduled task) so the burst timer tracks real work, not
    wall-clock. Call ``maybe_break()`` after each successful task —
    it sleeps a random break duration when the current burst is done.
    """

    def __init__(self, heartbeat: Heartbeat):
        self._heartbeat = heartbeat
        self._new_burst()

    def _new_burst(self):
        self._burst_start = time.monotonic()
        self._burst_duration = random.uniform(
            CAMPAIGN_CONFIG["burst_min_seconds"],
            CAMPAIGN_CONFIG["burst_max_seconds"],
        )

    def reset(self):
        """Start a fresh burst without taking a break. Use after idle gaps."""
        self._new_burst()

    def maybe_break(self):
        """Sleep a random break and start a new burst if the current one is done."""
        if time.monotonic() - self._burst_start < self._burst_duration:
            return
        break_seconds = random.uniform(
            CAMPAIGN_CONFIG["break_min_seconds"],
            CAMPAIGN_CONFIG["break_max_seconds"],
        )
        logger.info("Taking a %dm break", int(break_seconds // 60))
        sleep_with_heartbeat(
            break_seconds,
            self._heartbeat,
            f"on break, {int(break_seconds // 60)}m total",
        )
        self._new_burst()


def _build_qualifiers(campaigns, cfg):
    """Create a qualifier for every campaign, keyed by campaign PK."""
    from openoutreach.crm.models import Lead

    qualifiers: dict[int, BayesianQualifier] = {}
    n_regular = 0
    for campaign in campaigns:
        q = BayesianQualifier(
            seed=42,
            n_mc_samples=cfg["qualification_n_mc_samples"],
            campaign=campaign,
        )
        X, y = Lead.get_labeled_arrays(campaign)
        if len(X) > 0:
            q.warm_start(X, y)
            logger.info(
                colored("GP qualifier warm-started", "cyan")
                + " on %d labelled samples (%d positive, %d negative)"
                + " for campaign %s",
                len(y), int((y == 1).sum()), int((y == 0).sum()), campaign,
            )
        qualifiers[campaign.pk] = q
        n_regular += 1

    return qualifiers


# ------------------------------------------------------------------
# Active-hours schedule guard
# ------------------------------------------------------------------


def seconds_until_active(tz_name: str | None) -> float:
    """Return seconds to wait before the next active window, or 0 if active now.

    Single contiguous daily window — no weekend skip. Returns 0 (never gate)
    when active hours are disabled or ``tz_name`` is None — the timezone is
    resolved from the operator's LinkedIn profile post-login, and an unknown
    profile country leaves it None rather than guessing UTC.
    """
    if not ENABLE_ACTIVE_HOURS or tz_name is None:
        return 0.0
    tz = ZoneInfo(tz_name)
    now = timezone.localtime(timezone=tz)

    if ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR:
        return 0.0

    candidate = timezone.make_aware(
        now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0, tzinfo=None),
        timezone=tz,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()


# ------------------------------------------------------------------
# Checkpoint exit
# ------------------------------------------------------------------


def _exit_on_checkpoint(session, task, url: str) -> None:
    """Log loudly, mark the task failed, persist profile checkpoint status, close the session, and exit(1).

    Called when LinkedIn flags the account with a security checkpoint.
    We do NOT retry or reauthenticate — every retry hardens the block.
    The user clears the challenge in a real browser, then restarts the daemon.
    """
    prof_user = session.linkedin_profile.linkedin_username if (session and getattr(session, "linkedin_profile", None)) else "Unknown"
    logger.error(
        colored(
            f"ACCOUNT CHECKPOINTED — {prof_user}",
            "red", attrs=["bold"],
        )
    )
    logger.error("Clear the challenge in a real browser: %s", url)
    logger.error("Then restart the daemon.")

    # 1. Update profile session status to checkpoint
    if session and getattr(session, "linkedin_profile", None):
        try:
            prof = session.linkedin_profile
            prof.session_status = "checkpoint"
            prof.session_error_details = f"LinkedIn Checkpoint Challenge: {url}"
            prof.save(update_fields=["session_status", "session_error_details"])
        except Exception as e:
            logger.error("Failed to update profile session_status: %s", e)

    # 2. Create / ensure IncidentHold
    try:
        from django.contrib.contenttypes.models import ContentType
        from openoutreach.career.models import IncidentHold, IncidentHoldTarget
        from openoutreach.linkedin.models import LinkedInProfile
        hold, _ = IncidentHold.objects.get_or_create(
            reason="LINKEDIN_ACCOUNT_CHECKPOINT",
            defaults={
                "description": f"LinkedIn Security Checkpoint Challenge on profile {getattr(session, 'linkedin_profile_id', 'Unknown')}: {url}",
                "active": True
            }
        )
        if session and getattr(session, "linkedin_profile", None):
            ct = ContentType.objects.get_for_model(LinkedInProfile)
            IncidentHoldTarget.objects.get_or_create(
                incident_hold=hold,
                content_type=ct,
                object_id=session.linkedin_profile.id
            )
    except Exception as e:
        logger.error("Failed to create IncidentHold: %s", e)

    # 3. Mark task failed with non-retriable checkpoint reason
    if task:
        task.mark_failed(reason="linkedin_checkpoint", retriable=False)

    if session:
        try:
            session.close()
        except Exception:
            pass
    sys.exit(1)


# ------------------------------------------------------------------
# Task queue worker
# ------------------------------------------------------------------


def run_daemon(session):
    import os
    from openoutreach.core.models import Campaign, DaemonStatus
    import atexit
    from django.utils import timezone

    hb_thread = HeartbeatThread(interval=10.0)
    hb_thread.start()

    def cleanup():
        try:
            hb_thread.stop()
            DaemonStatus.objects.filter(pk=1).update(is_running=False)
        except Exception:
            pass
    atexit.register(cleanup)

    try:
        status, _ = DaemonStatus.objects.get_or_create(pk=1)
        status.is_running = True
        status.pid = os.getpid()
        status.active_profile_username = session.linkedin_profile.linkedin_username if (session and session.linkedin_profile) else ""
        status.last_heartbeat = timezone.now()
        status.last_error = ""
        status.save()
    except Exception:
        pass

    cfg = CAMPAIGN_CONFIG

    qualifiers = _build_qualifiers(session.campaigns, cfg)

    campaigns = session.campaigns or ([session.campaign] if getattr(session, "campaign", None) else [])
    if not campaigns:
        logger.error("No campaigns found — cannot start daemon")
        hb_thread.stop()
        return

    logger.info(
        colored("HuntFlow daemon started.", "green", attrs=["bold"])
        + " — %d campaigns, task queue worker",
        len(campaigns),
    )

    if ENABLE_ACTIVE_HOURS:
        logger.info(
            "Active hours %02d:00–%02d:00 — timezone %s",
            ACTIVE_START_HOUR, ACTIVE_END_HOUR, session.active_timezone_provenance(),
        )
    else:
        logger.info("Active hours disabled — running 24/7")

    heartbeat = Heartbeat()
    rhythm = _HumanRhythmBreak(heartbeat)

    from openoutreach.core.scheduler import reconcile
    reconcile(session)

    # Single-threaded: one task at a time, no concurrent enqueuing,
    # so sleeping until the next scheduled_at is safe.
    while True:
        pause = seconds_until_active(session.active_timezone)
        if pause > 0:
            logger.info(
                "Outside active hours (%02d:00–%02d:00 %s) — next window in %s",
                ACTIVE_START_HOUR, ACTIVE_END_HOUR,
                session.active_timezone, _hm(pause),
            )
            sleep_with_heartbeat(
                pause, heartbeat,
                lambda left: f"outside active hours, {_hm(left)} left",
            )
            rhythm.reset()
            continue

        task = Task.objects.claim_next()
        if task is None:
            # Check workspace safety status before calling reconcile
            ws = None
            lp = getattr(session, "linkedin_profile", None)
            if lp and getattr(lp, "workspace", None):
                ws = lp.workspace
            elif getattr(session, "campaign", None) and session.campaign.workspace:
                ws = session.campaign.workspace

            from openoutreach.core.models import WorkspaceAutomationSettings
            auto_st = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None
            is_safety_stopped = bool(
                auto_st and (
                    auto_st.soak_status == "SAFETY_STOP"
                    or not auto_st.master_automation_enabled
                    or getattr(auto_st, "provider_safety_state", "NORMAL") != "NORMAL"
                    or getattr(auto_st, "manual_provider_use_paused", False)
                )
            )
            if lp:
                try:
                    lp.refresh_from_db(fields=["session_status", "active"])
                except Exception:
                    pass
                if not getattr(lp, "active", True) or getattr(lp, "session_status", "valid") in [
                    "needs_reauth", "expired", "invalid", "checkpoint", "restricted", "challenge_required"
                ]:
                    is_safety_stopped = True

            if is_safety_stopped:
                # Safe idle heartbeat / backoff — do NOT spin reconcile every second!
                sleep_with_heartbeat(5, heartbeat, "safety hold active — queue idle")
                rhythm.reset()
                continue

            # Nothing ready — reconcile the queue from CRM state. Any deal
            # stuck without a pending task (e.g. because a prior handler
            # crashed) gets a fresh task here; this is the retry mechanism.
            from openoutreach.core.scheduler import reconcile
            reconcile(session)

            wait = Task.objects.seconds_to_next()
            if wait is None:
                logger.info("Queue empty after reconcile — sleeping 1s")
                sleep_with_heartbeat(1, heartbeat, "queue empty")
                rhythm.reset()
                continue
            if wait > 0:
                logger.info("Next task in %s — sleeping", _hm(wait))
                sleep_with_heartbeat(
                    min(wait, 2.0), heartbeat, lambda left: f"next task in {_hm(left)}",
                )
                rhythm.reset()
            continue

        cid = task.payload.get("campaign_id")
        if cid is not None:
            campaign = Campaign.objects.filter(pk=cid).first()
            if not campaign:
                logger.error("Campaign %s not found", cid)
                task.mark_failed(reason="campaign_not_found")
                continue
            session.campaign = campaign
        elif task.task_type not in [Task.TaskType.HEALTH_CHECK, Task.TaskType.JOB_APPLICATION, Task.TaskType.JOB_DISCOVERY]:
            logger.error("No campaign_id in payload for task %s", task.pk)
            task.mark_failed(reason="missing_campaign_id")
            continue
        task.mark_running()

        # Check safety holds & verification for the session user / candidate
        from openoutreach.career.models import CandidateProfile
        from openoutreach.career.service import check_safety_holds_and_verification
        cand_profile = None
        if task.task_type == Task.TaskType.JOB_DISCOVERY:
            cid = task.payload.get("candidate_id")
            if cid:
                cand_profile = CandidateProfile.objects.filter(pk=cid).first()
        elif task.task_type == Task.TaskType.JOB_APPLICATION:
            app_id = task.payload.get("application_id")
            if app_id:
                from openoutreach.career.models import JobApplication
                app = JobApplication.objects.filter(pk=app_id).first()
                if app:
                    cand_profile = app.candidate_profile
            if not cand_profile:
                cid = task.payload.get("candidate_id")
                if cid:
                    cand_profile = CandidateProfile.objects.filter(pk=cid).first()

        if not cand_profile and getattr(session, "django_user", None):
            from django.contrib.auth.models import User
            if isinstance(session.django_user, User):
                cand_profile = CandidateProfile.objects.filter(user=session.django_user).first()

        try:
            check_safety_holds_and_verification(profile=cand_profile)
        except Exception as e:
            logger.error("Safety hold or verification block active for candidate profile: %s", e)
            task.mark_failed(reason=f"safety_hold_blocked: {str(e)}", retriable=False)
            continue

        # Account-level gate for LinkedIn tasks when profile is checkpointed or blocked
        if task.task_type in [Task.TaskType.CONNECT, Task.TaskType.FOLLOW_UP, Task.TaskType.CHECK_PENDING, Task.TaskType.JOB_APPLICATION] and not task.payload.get("mock_boundary"):
            prof = getattr(session, "linkedin_profile", None)
            if prof:
                prof.refresh_from_db()
                if prof.session_status in ["checkpoint", "needs_reauth", "restricted", "invalid"] or not prof.can_execute(task.task_type):
                    logger.warning(
                        "BLOCKED: LinkedIn profile %s has session_status=%s. Gating %s task #%s without external execution.",
                        prof.linkedin_username, prof.session_status, task.task_type, task.pk
                    )
                    task.mark_failed(reason="linkedin_checkpoint", retriable=False)
                    continue

        handler = _HANDLERS.get(task.task_type)
        if handler is None:
            logger.error("Unknown task type: %s", task.task_type)
            task.mark_failed(reason="unknown_task_type")
            continue

        try:
            with failure_diagnostics(session):
                handler(task, session, qualifiers)
        except CheckpointChallengeError as exc:
            try:
                from openoutreach.core.models import DaemonStatus
                DaemonStatus.objects.filter(pk=1).update(is_running=False, last_error=f"LinkedIn Checkpoint Challenge: {exc.url}")
            except Exception:
                pass
            _exit_on_checkpoint(session, task, exc.url)
        except AuthenticationError:
            logger.warning("Session expired during %s — re-authenticating", task)
            try:
                session.reauthenticate()
            except CheckpointChallengeError as exc:
                try:
                    from openoutreach.core.models import DaemonStatus
                    DaemonStatus.objects.filter(pk=1).update(is_running=False, last_error=f"LinkedIn Checkpoint Challenge: {exc.url}")
                except Exception:
                    pass
                _exit_on_checkpoint(session, task, exc.url)
            except Exception:
                logger.exception("Re-authentication failed for %s", task)
            # Either way, mark this task FAILED; reconcile will re-create a
            # fresh task for the deal on the next idle cycle.
            task.mark_failed()
            continue
        except ModelHTTPError as e:
            try:
                from openoutreach.core.models import DaemonStatus
                DaemonStatus.objects.filter(pk=1).update(is_running=False, last_error=f"LLM API Error: {str(e)}")
            except Exception:
                pass
            task.mark_failed()
            logger.error(
                "Daemon stopped - LLM API error\n%s\nCheck ai_model (provider:model), llm_api_key, and llm_api_base in Admin -> Site Configuration.", e
            )
            return
        except Exception:
            task.mark_failed()
            logger.exception("Task %s failed", task)
            continue

        task.mark_completed()
        rhythm.maybe_break()
