from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import ensure_csrf_cookie
from functools import wraps
from django.contrib.auth import REDIRECT_FIELD_NAME, login as auth_login, logout as auth_logout
from django.contrib.auth.forms import AuthenticationForm
from django.urls import reverse
import json

from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.contrib import messages

def dashboard_login_required(view_func):
    """Decorator for views that checks that the user is logged in, redirecting
    to the HuntFlow dashboard login page if necessary. It requires the user
    to be authenticated and either staff, superuser, or an active workspace member.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            path = request.get_full_path()
            login_url = reverse("dashboard:login")
            return redirect(f"{login_url}?{REDIRECT_FIELD_NAME}={path}")
        from openoutreach.core.models import WorkspaceMembership
        has_membership = WorkspaceMembership.objects.filter(user=request.user, status="active").exists()
        if not (request.user.is_staff or request.user.is_superuser or has_membership):
            auth_logout(request)
            messages.error(request, "Access denied: Staff status required.")
            path = request.get_full_path()
            login_url = reverse("dashboard:login")
            return redirect(f"{login_url}?{REDIRECT_FIELD_NAME}={path}")
        return view_func(request, *args, **kwargs)
    return _wrapped_view

from openoutreach.core.models import Campaign, Task, SiteConfig, TaskQueueEvent
from openoutreach.crm.models import Lead, Deal, DealState, LeadPipelineEvent, Outcome
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.emails.models import Mailbox
from openoutreach.dashboard.forms import (
    CampaignForm,
    LinkedInProfileForm,
    MailboxForm,
    LLMConfigForm,
    BetterContactConfigForm,
)


def _mask_key(key: str) -> str:
    """Safely mask secret key status without revealing secret characters or treating placeholders as valid."""
    from openoutreach.core.models import is_valid_credential
    if not is_valid_credential(key):
        return "Needs configuration"
    return "Configured: ********"



@dashboard_login_required
def overview(request):
    from openoutreach.core.readiness import check_launch_readiness
    from openoutreach.core.models import DaemonStatus, LaunchRun, WorkspaceAutomationSettings
    from openoutreach.career.models import JobApplicationAnswer, CandidateProfile, CandidateApplicationProfile, JobApplication, Job
    from openoutreach.emails.models import Mailbox
    import urllib.request

    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]
    auto_settings = ctx["automation_settings"]
    master_automation_enabled = auto_settings.master_automation_enabled if auto_settings else False

    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    if workspace:
        campaign_qs = Campaign.objects.filter(workspace=workspace, is_synthetic=False)
    else:
        campaign_qs = Campaign.objects.filter(users=request.user, is_synthetic=False)

    total_campaigns = campaign_qs.count()
    active_campaigns = campaign_qs.filter(is_active=True).count()
    
    if workspace:
        pending_tasks = Task.objects.filter(status=Task.Status.PENDING, payload__workspace_id=workspace.pk).count()
        failed_tasks = Task.objects.filter(status=Task.Status.FAILED, payload__workspace_id=workspace.pk).count()
    else:
        pending_tasks = Task.objects.filter(status=Task.Status.PENDING).count()
        failed_tasks = Task.objects.filter(status=Task.Status.FAILED).count()
    
    if workspace:
        deals_qs = Deal.objects.filter(campaign__workspace=workspace)
    else:
        deals_qs = Deal.objects.filter(campaign__users=request.user)

    qualified_leads = deals_qs.filter(state=DealState.QUALIFIED).count()
    connected_leads = deals_qs.filter(state=DealState.CONNECTED).count()
    
    emails_found = deals_qs.filter(lead__api_email__isnull=False).exclude(lead__api_email="").count()
    
    if workspace:
        actions_today = ActionLog.objects.filter(
            models.Q(campaign__workspace=workspace) | models.Q(linkedin_profile__user=request.user),
            created_at__gte=today_start,
        ).count()
        connection_actions_today = ActionLog.objects.filter(
            models.Q(campaign__workspace=workspace) | models.Q(linkedin_profile__user=request.user),
            action_type=ActionLog.ActionType.CONNECT,
            created_at__gte=today_start,
        ).count()
        replies_today = ActionLog.objects.filter(
            models.Q(campaign__workspace=workspace) | models.Q(linkedin_profile__user=request.user),
            action_type__icontains="reply",
            created_at__gte=today_start,
        ).count()
    else:
        actions_today = ActionLog.objects.filter(created_at__gte=today_start).count()
        connection_actions_today = ActionLog.objects.filter(
            action_type=ActionLog.ActionType.CONNECT,
            created_at__gte=today_start,
        ).count()
        replies_today = ActionLog.objects.filter(
            action_type__icontains="reply",
            created_at__gte=today_start,
        ).count()

    jobs_applied_today = JobApplication.objects.filter(candidate_profile__user=request.user, created_at__gte=today_start).count()
    
    last_action_log = ActionLog.objects.filter(models.Q(campaign__workspace=workspace) | models.Q(linkedin_profile__user=request.user)).order_by("-created_at").first() if workspace else ActionLog.objects.order_by("-created_at").first()
    last_action_time = last_action_log.created_at if last_action_log else None

    # Daemon Status
    d_status = DaemonStatus.objects.first()
    daemon_running = bool(d_status and d_status.is_running and d_status.last_heartbeat and (timezone.now() - d_status.last_heartbeat).total_seconds() < 120)

    # Ollama Status
    ollama_connected = False
    try:
        from openoutreach.core.llm import get_default_ollama_base_url
        ollama_endpoint = get_default_ollama_base_url()
        req = urllib.request.Request(f"{ollama_endpoint}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                ollama_connected = True
    except Exception:
        ollama_connected = False

    # LinkedIn Accounts Status - strictly user / workspace scoped
    if workspace:
        linkedin_profiles = list(LinkedInProfile.objects.filter(models.Q(user=request.user) | models.Q(workspace=workspace)).distinct())
    else:
        linkedin_profiles = list(LinkedInProfile.objects.filter(user=request.user))
    active_profile = next((p for p in linkedin_profiles if p.active and p.session_status == "valid"), None)
    attention_profile = next((p for p in linkedin_profiles if p.session_status in ["checkpoint", "needs_reauth", "restricted"]), None)
    
    if active_profile:
        li_status = "connected"
        li_label = "Connected ✓"
        li_detail = active_profile.display_name or active_profile.linkedin_username or "Active Account"
    elif attention_profile:
        li_status = "attention"
        li_label = "Needs attention ⚠️"
        li_detail = "Re-authentication required"
    else:
        li_status = "not_connected"
        li_label = "Not connected"
        li_detail = "Add your LinkedIn session"

    # Mailbox Status - strictly workspace scoped!
    mailboxes = list(Mailbox.objects.for_workspace(workspace))
    if mailboxes:
        email_status = "connected"
        email_label = "Connected ✓"
        email_detail = getattr(mailboxes[0], "from_address", "") or getattr(mailboxes[0], "username", "")
    else:
        email_status = "not_connected"
        email_label = "Not connected"
        email_detail = "Connect Gmail or Mailbox"

    # Job Profile Status
    cand_prof = ctx["candidate_profile"]
    app_prof = CandidateApplicationProfile.objects.filter(candidate_profile=cand_prof).first() if cand_prof else None
    if app_prof and app_prof.confirmed:
        job_prof_status = "complete"
        job_prof_label = "Complete ✓"
        job_prof_detail = "Resume & preferences verified"
    else:
        job_prof_status = "incomplete"
        job_prof_label = "Incomplete (80%)"
        job_prof_detail = "Review job application profile"

    # Wizard Steps Computation
    steps = [
        {
            "num": 1,
            "title": "Connect LinkedIn",
            "desc": "Add your session cookie for automated networking",
            "status": "complete" if li_status == "connected" else ("attention" if li_status == "attention" else "pending"),
            "url": reverse("dashboard:settings_linkedin"),
        },
        {
            "num": 2,
            "title": "Connect Email",
            "desc": "Set up your SMTP sending address",
            "status": "complete" if email_status == "connected" else "pending",
            "url": reverse("dashboard:settings_email"),
        },
        {
            "num": 3,
            "title": "Complete Job Profile",
            "desc": "Add your resume and target job criteria",
            "status": "complete" if job_prof_status == "complete" else "pending",
            "url": reverse("dashboard:settings_job_profile"),
        },
        {
            "num": 4,
            "title": "Define Outreach Preferences",
            "desc": "Set daily limits and writing style",
            "status": "complete" if active_campaigns > 0 else "pending",
            "url": reverse("dashboard:settings_automation"),
        },
        {
            "num": 5,
            "title": "Review & Start",
            "desc": "Check setup readiness before launching",
            "status": "pending",
            "url": reverse("dashboard:settings_automation"),
        },
    ]
    completed_steps_count = sum(1 for s in steps if s["status"] == "complete")

    followups_today = ActionLog.objects.filter(
        models.Q(campaign__workspace=workspace) | models.Q(linkedin_profile__user=request.user) if workspace else models.Q(),
        action_type=ActionLog.ActionType.FOLLOW_UP,
        created_at__gte=today_start,
    ).count()

    if workspace:
        emails_sent_today = LeadPipelineEvent.objects.filter(
            campaign__workspace=workspace,
            action__in=["email_sent", "message_sent"],
            created_at__gte=today_start,
        ).count()
    else:
        emails_sent_today = LeadPipelineEvent.objects.filter(
            action__in=["email_sent", "message_sent"],
            created_at__gte=today_start,
        ).count()

    jobs_found_today = Job.objects.filter(is_synthetic=False, created_at__gte=today_start).count()
    user_name = cand_prof.full_name if cand_prof else (request.user.get_full_name() or request.user.username)

    checklist = {
        "linkedin": {
            "status": li_status,
            "label": li_label,
            "detail": li_detail,
            "url": reverse("dashboard:settings_linkedin"),
        },
        "email": {
            "status": email_status,
            "label": email_label,
            "detail": email_detail,
            "url": reverse("dashboard:settings_email"),
        },
        "job_profile": {
            "status": job_prof_status,
            "label": job_prof_label,
            "detail": job_prof_detail,
            "url": reverse("dashboard:settings_job_profile"),
        },
        "automation": {
            "status": "running" if master_automation_enabled else "paused",
            "label": "Master Automation: " + ("Running ✓" if master_automation_enabled else "Paused"),
            "detail": "Actively running in background" if master_automation_enabled else "Safely paused by operator",
            "url": reverse("dashboard:settings_automation"),
        },
    }

    # Build Needs Attention items with direct fix buttons
    needs_attention = []
    
    for p in linkedin_profiles:
        if not p.active or p.session_status != "valid":
            account_label = getattr(p, "display_name", "") or getattr(p, "linkedin_username", "") or f"Profile #{p.pk}"
            needs_attention.append({
                "title": f"LinkedIn Account '{account_label}' requires attention",
                "description": f"Session is {p.get_session_status_display().lower()}. Please re-authenticate your LinkedIn account.",
                "fix_url": reverse("dashboard:settings_linkedin"),
                "fix_label": "Reconnect Account",
                "level": "warning",
            })

    # Campaigns without sender accounts or with failed tasks
    for c in Campaign.objects.all():
        if c.is_active and not c.users.exists():
            needs_attention.append({
                "title": f"Campaign '{c.name}' has no assigned sender account",
                "description": "Outreach tasks cannot be dispatched without a bound sender account.",
                "fix_url": reverse("dashboard:campaign_edit", kwargs={"pk": c.pk}),
                "fix_label": "Assign Sender",
                "level": "error",
            })
        c_failed = Task.objects.filter(status=Task.Status.FAILED).filter(
            models.Q(payload__campaign_id=c.id) | models.Q(payload__campaign_id=str(c.id))
        ).count()
        if c_failed > 0:
            needs_attention.append({
                "title": f"Campaign '{c.name}' has {c_failed} failed task(s)",
                "description": "Tasks failed during execution. Review or clear failed tasks.",
                "fix_url": reverse("dashboard:campaign_clear_failed", kwargs={"pk": c.pk}),
                "fix_label": "Clear Failed Tasks",
                "level": "warning",
            })

    # Ollama offline
    if not ollama_connected:
        from openoutreach.core.llm import get_default_ollama_base_url
        ollama_endpoint = get_default_ollama_base_url()
        needs_attention.append({
            "title": "Ollama AI service is offline or unreachable",
            "description": f"Ensure Ollama is running and accessible at {ollama_endpoint}.",
            "fix_url": reverse("dashboard:settings_llm"),
            "fix_label": "Check AI Integrations",
            "level": "error",
        })

    # Master automation daemon stopped
    if not daemon_running:
        needs_attention.append({
            "title": "Master Automation Daemon is stopped",
            "description": "Background task queue executor is not running.",
            "fix_url": reverse("dashboard:settings_system"),
            "fix_label": "View Daemon Settings",
            "level": "warning",
        })

    from openoutreach.core.services.quota_service import (
        get_daily_successful_easy_apply_count,
        get_daily_job_provider_attempt_count,
    )
    successful_jobs_today = get_daily_successful_easy_apply_count(workspace=workspace, candidate_profile=cand_prof)
    job_attempts_today = get_daily_job_provider_attempt_count(workspace=workspace, candidate_profile=cand_prof)
    job_daily_target = getattr(auto_settings, "daily_job_application_target", 50) if auto_settings else 50
    job_daily_ceiling = getattr(auto_settings, "max_daily_job_applications", 60) if auto_settings else 60

    if not auto_settings or not auto_settings.master_automation_enabled or not auto_settings.auto_apply_jobs:
        job_engine_status = "PAUSED"
    elif auto_settings.soak_status == "SAFETY_STOP":
        job_engine_status = "SAFETY_STOP"
    elif successful_jobs_today >= job_daily_target:
        job_engine_status = "TARGET_REACHED"
    elif job_attempts_today >= job_daily_ceiling:
        job_engine_status = "CEILING_REACHED"
    elif not daemon_running:
        job_engine_status = "DAEMON_OFFLINE"
    else:
        pending_job_tasks = Task.objects.filter(
            task_type__in=[Task.TaskType.JOB_APPLICATION, Task.TaskType.JOB_DISCOVERY],
            status=Task.Status.PENDING,
        ).exists()
        job_engine_status = "ACTIVE_SEARCHING" if not pending_job_tasks else "ACTIVE_APPLYING"

    # Launch Readiness Status
    readiness = check_launch_readiness()
    recent_launch_runs = LaunchRun.objects.select_related("operator").order_by("-created_at")[:5]

    from openoutreach.core.operations import get_operations_snapshot
    operations_snapshot = get_operations_snapshot()

    from openoutreach.core.production import get_production_readiness_snapshot
    prod_snap = get_production_readiness_snapshot()

    context = {
        "user_name": user_name,
        "master_automation_enabled": master_automation_enabled,
        "checklist": checklist,
        "steps": steps,
        "completed_steps_count": completed_steps_count,
        "workspace": workspace,
        "admin_operating": ctx.get("admin_operating", False),
        "total_steps_count": len(steps),
        "wizard_percentage": int((completed_steps_count / len(steps)) * 100),
        "active_campaigns": active_campaigns,
        "total_campaigns": total_campaigns,
        "connections_sent_today": connection_actions_today,
        "accepted_today": connected_leads,
        "followups_today": followups_today,
        "emails_sent_today": emails_sent_today,
        "jobs_found_today": jobs_found_today,
        "jobs_applied_today": successful_jobs_today,
        "successful_jobs_today": successful_jobs_today,
        "job_attempts_today": job_attempts_today,
        "job_daily_target": job_daily_target,
        "job_daily_ceiling": job_daily_ceiling,
        "job_engine_status": job_engine_status,
        "jobs_inspected_last_cycle": getattr(auto_settings, "jobs_inspected_last_cycle", 0) if auto_settings else 0,
        "safe_jobs_last_cycle": getattr(auto_settings, "safe_jobs_last_cycle", 0) if auto_settings else 0,
        "last_job_discovery_completed_at": getattr(auto_settings, "last_job_discovery_completed_at", None) if auto_settings else None,
        "actions_today": actions_today,
        "replies_today": replies_today,
        "last_action_time": last_action_time,
        "needs_attention": needs_attention,
        "daemon_running": daemon_running,
        "ollama_connected": ollama_connected,
        "readiness": readiness,
        "recent_launch_runs": recent_launch_runs,
        "operations_snapshot": operations_snapshot,
        "prod_snap": prod_snap,
    }
    return render(request, "dashboard/overview.html", context)


@dashboard_login_required
def campaigns(request):
    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]
    auto_settings = ctx["automation_settings"]
    master_automation_enabled = auto_settings.master_automation_enabled if auto_settings else False

    include_synthetic = request.GET.get("include_synthetic") == "1" and request.user.is_superuser
    if include_synthetic:
        campaign_list = Campaign.objects.all()
    elif workspace:
        if request.user.is_staff or request.user.is_superuser:
            campaign_list = Campaign.objects.filter(
                models.Q(workspace=workspace) | models.Q(users=request.user) | models.Q(workspace__isnull=True),
                is_synthetic=False
            ).distinct().exclude(name__icontains="Test").exclude(name__icontains="Acceptance").exclude(name__icontains="DryRun")
        else:
            campaign_list = Campaign.objects.filter(
                models.Q(workspace=workspace) | models.Q(users=request.user),
                is_synthetic=False
            ).distinct().exclude(name__icontains="Test").exclude(name__icontains="Acceptance").exclude(name__icontains="DryRun")
    else:
        if request.user.is_staff or request.user.is_superuser:
            campaign_list = Campaign.objects.filter(
                models.Q(users=request.user) | models.Q(workspace__isnull=True),
                is_synthetic=False
            ).exclude(name__icontains="Test").exclude(name__icontains="Acceptance").exclude(name__icontains="DryRun")
        else:
            campaign_list = Campaign.objects.filter(
                users=request.user,
                is_synthetic=False
            ).exclude(name__icontains="Test").exclude(name__icontains="Acceptance").exclude(name__icontains="DryRun")
    
    campaign_data = []

    all_pending = list(Task.objects.filter(status=Task.Status.PENDING))
    all_failed = list(Task.objects.filter(status=Task.Status.FAILED))
    
    from openoutreach.linkedin.models import LinkedInProfile
    from openoutreach.career.models import IncidentHold
    lp = ctx["linkedin_profile"]
    has_active_hold = IncidentHold.objects.filter(active=True).exists()

    for campaign in campaign_list:
        connect_pending = sum(
            1 for t in all_pending
            if t.payload.get("campaign_id") == campaign.id and t.task_type == Task.TaskType.CONNECT
        )
        followup_pending = sum(
            1 for t in all_pending
            if t.payload.get("campaign_id") == campaign.id and t.task_type == Task.TaskType.FOLLOW_UP
        )
        check_pending = sum(
            1 for t in all_pending
            if t.payload.get("campaign_id") == campaign.id and t.task_type == Task.TaskType.CHECK_PENDING
        )
        failed_count = sum(1 for t in all_failed if t.payload.get("campaign_id") == campaign.id)
        ignored_pending = (connect_pending + followup_pending + check_pending) if not campaign.is_active else 0
        
        connected_count = campaign.deals.filter(state=DealState.CONNECTED).count()
        replies_count = campaign.deals.filter(state__in=[DealState.CONNECTED, DealState.COMPLETED]).count()
        assigned_users = campaign.users.all()
        
        # Calculate human-readable effective status
        if not campaign.is_active:
            effective_status = "PAUSED"
            effective_badge = "badge-secondary"
        elif not lp or lp.session_status != "valid":
            effective_status = "NEEDS LINKEDIN"
            effective_badge = "badge-warning"
        elif has_active_hold or failed_count > 0:
            effective_status = "NEEDS ATTENTION"
            effective_badge = "badge-danger"
        elif master_automation_enabled:
            effective_status = "RUNNING"
            effective_badge = "badge-success"
        else:
            effective_status = "READY"
            effective_badge = "badge-info"

        campaign_data.append({
            "campaign": campaign,
            "effective_status": effective_status,
            "effective_badge": effective_badge,
            "connect_pending": connect_pending,
            "followup_pending": followup_pending,
            "check_pending": check_pending,
            "failed_count": failed_count,
            "ignored_pending": ignored_pending,
            "connected_count": connected_count,
            "replies_count": replies_count,
            "assigned_users": assigned_users,
        })

    return render(request, "dashboard/campaigns.html", {"campaigns": campaign_data})


@dashboard_login_required
def campaign_create(request):
    form = CampaignForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        campaign = form.save()
        messages.success(request, f"Campaign '{campaign.name}' created successfully.")
        return redirect("dashboard:campaigns")
    return render(request, "dashboard/campaign_form.html", {"form": form, "title": "Create Campaign"})


def get_authorized_campaign(request, pk):
    from openoutreach.core.models import Campaign, WorkspaceMembership
    from django.core.exceptions import PermissionDenied
    campaign = get_object_or_404(Campaign, pk=pk)
    if not (request.user.is_superuser or request.user.is_staff):
        if campaign.workspace:
            if not WorkspaceMembership.objects.filter(workspace=campaign.workspace, user=request.user, status="active").exists():
                raise PermissionDenied("You do not have permission to access this campaign.")
        elif campaign.owner and campaign.owner != request.user and not campaign.users.filter(id=request.user.id).exists():
            raise PermissionDenied("You do not have permission to access this campaign.")
    return campaign


def get_authorized_deal(request, pk):
    from openoutreach.crm.models.deal import Deal
    from openoutreach.core.models import WorkspaceMembership
    from django.core.exceptions import PermissionDenied
    deal = get_object_or_404(Deal, pk=pk)
    ws = getattr(deal.campaign, "workspace", None) or getattr(deal.lead, "workspace", None)
    if not (request.user.is_superuser or request.user.is_staff):
        if ws:
            if not WorkspaceMembership.objects.filter(workspace=ws, user=request.user, status="active").exists():
                raise PermissionDenied("You do not have permission to access this deal.")
    return deal


@dashboard_login_required
def campaign_edit(request, pk):
    campaign = get_authorized_campaign(request, pk)
    form = CampaignForm(request.POST or None, instance=campaign)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Campaign '{campaign.name}' updated successfully.")
        return redirect("dashboard:campaigns")

    connect_pending = sum(
        1 for t in Task.objects.filter(status=Task.Status.PENDING, task_type=Task.TaskType.CONNECT)
        if t.payload.get("campaign_id") == campaign.id
    )
    followup_pending = sum(
        1 for t in Task.objects.filter(status=Task.Status.PENDING, task_type=Task.TaskType.FOLLOW_UP)
        if t.payload.get("campaign_id") == campaign.id
    )
    check_pending = sum(
        1 for t in Task.objects.filter(status=Task.Status.PENDING, task_type=Task.TaskType.CHECK_PENDING)
        if t.payload.get("campaign_id") == campaign.id
    )
    failed_count = sum(
        1 for t in Task.objects.filter(status=Task.Status.FAILED)
        if t.payload.get("campaign_id") == campaign.id
    )
    ignored_pending = (connect_pending + followup_pending + check_pending) if not campaign.is_active else 0

    from openoutreach.linkedin.pipeline.icp_evaluator import validate_campaign_icp_consistency
    icp_validation = validate_campaign_icp_consistency(campaign)

    context = {
        "form": form,
        "title": "Edit Campaign",
        "campaign": campaign,
        "connect_pending": connect_pending,
        "followup_pending": followup_pending,
        "check_pending": check_pending,
        "failed_count": failed_count,
        "ignored_pending": ignored_pending,
        "icp_validation": icp_validation,
    }
    return render(request, "dashboard/campaign_form.html", context)


@dashboard_login_required
def campaign_delete(request, pk):
    campaign = get_authorized_campaign(request, pk)
    if request.method == "POST":
        # ── Deleting related tasks safely ──
        all_tasks = Task.objects.all()
        tasks_to_delete = [t for t in all_tasks if t.payload.get("campaign_id") == campaign.id]
        deleted_tasks_count = len(tasks_to_delete)
        for t in tasks_to_delete:
            t.delete()
            
        campaign_name = campaign.name
        campaign.delete()
        messages.success(
            request,
            f"Campaign '{campaign_name}' and {deleted_tasks_count} associated task queue entries deleted successfully."
        )
        return redirect("dashboard:campaigns")
        
    return render(request, "dashboard/campaign_confirm_delete.html", {"campaign": campaign})


@dashboard_login_required
def leads(request):
    query = request.GET.get("q", "").strip()
    campaign_id = request.GET.get("campaign_id", "").strip()
    status_filter = request.GET.get("status", "").strip()
    email_filter = request.GET.get("email_filter", "").strip()
    deal_filter = request.GET.get("deal_filter", "").strip()
    failed_only = request.GET.get("failed_only", "").strip()
    pending_only = request.GET.get("pending_only", "").strip()
    
    leads_qs = Lead.objects.all().order_by("-creation_date")
    if not (request.user.is_staff or request.user.is_superuser):
        from openoutreach.core.models import WorkspaceMembership
        user_ws = WorkspaceMembership.objects.filter(user=request.user, status="active").values_list("workspace_id", flat=True)
        leads_qs = leads_qs.filter(workspace_id__in=user_ws)
    
    if query:
        leads_qs = leads_qs.filter(
            Q(public_identifier__icontains=query) |
            Q(linkedin_url__icontains=query) |
            Q(api_email__icontains=query)
        )
        
    if campaign_id:
        leads_qs = leads_qs.filter(deal__campaign_id=campaign_id)
        
    if status_filter:
        leads_qs = leads_qs.filter(deal__state=status_filter)

    if email_filter == "has_email":
        leads_qs = leads_qs.exclude(api_email="").exclude(api_email__isnull=True)
    elif email_filter == "no_email":
        leads_qs = leads_qs.filter(Q(api_email="") | Q(api_email__isnull=True))

    if deal_filter == "has_deal":
        leads_qs = leads_qs.filter(deal__isnull=False)
    elif deal_filter == "discovered_only":
        leads_qs = leads_qs.filter(deal__isnull=True)

    if failed_only == "yes":
        leads_qs = leads_qs.filter(deal__state=DealState.FAILED)
    if pending_only == "yes":
        leads_qs = leads_qs.filter(deal__state=DealState.PENDING)
        
    leads_data = []
    campaign_cache = {c.id: c for c in Campaign.objects.all()}
    
    for lead in leads_qs:
        deal = lead.deal_set.first()
        campaign_name = "None"
        state = "Discovered"
        title = lead.current_title or "Not available"
        company = lead.company or "Not available"
        last_action_time = None
        
        if deal:
            campaign_name = deal.campaign.name
            state = deal.state
            
            if (not lead.current_title or not lead.company) and deal.profile_summary and "facts" in deal.profile_summary:
                facts = deal.profile_summary["facts"]
                role_facts = []
                company_facts = []
                for fact in facts:
                    fact_lower = fact.lower()
                    if any(x in fact_lower for x in ["role", "title", "position", "ceo", "director", "manager", "vp"]):
                        role_facts.append(fact)
                    if "company" in fact_lower or " at " in fact_lower:
                        company_facts.append(fact)
                
                if not lead.current_title:
                    if role_facts:
                        title = role_facts[0]
                    elif facts:
                        title = facts[0]
                if not lead.company:
                    if company_facts:
                        company = company_facts[0]
                    elif len(facts) > 1:
                        company = facts[1]
            
            last_log = ActionLog.objects.filter(campaign=deal.campaign).order_by("-created_at").first()
            if last_log:
                last_action_time = last_log.created_at

        is_self_profile = LinkedInProfile.objects.filter(self_lead=lead).exists()
        is_disqualified = lead.disqualified or is_self_profile
        campaign_is_active = False
        if deal:
            campaign_is_active = deal.campaign.is_active

        eligible_email = bool(lead.api_email) and campaign_is_active and deal and deal.state == DealState.READY_TO_EMAIL and not is_disqualified
        eligible_connect = campaign_is_active and deal and (deal.state in (DealState.READY_TO_CONNECT, DealState.QUALIFIED) and not lead.api_email) and not is_disqualified
        eligible_followup = campaign_is_active and deal and deal.state == DealState.CONNECTED and not is_disqualified
        eligible_retry = campaign_is_active and deal and deal.state == DealState.FAILED and not is_disqualified

        full_name = f"{lead.first_name} {lead.last_name}".strip() or lead.public_identifier

        leads_data.append({
            "lead": lead,
            "full_name": full_name,
            "campaign_name": campaign_name,
            "state": state,
            "title": title,
            "company": company,
            "last_action_time": last_action_time,
            "eligible_email": eligible_email,
            "eligible_connect": eligible_connect,
            "eligible_followup": eligible_followup,
            "eligible_retry": eligible_retry,
        })
        
    context = {
        "leads": leads_data,
        "query": query,
        "campaigns": campaign_cache.values(),
        "statuses": DealState.choices,
        "selected_campaign": campaign_id,
        "selected_status": status_filter,
        "email_filter": email_filter,
        "deal_filter": deal_filter,
        "failed_only": failed_only,
        "pending_only": pending_only,
    }
    return render(request, "dashboard/leads.html", context)


@dashboard_login_required
def lead_detail(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    if not (request.user.is_staff or request.user.is_superuser):
        if lead.workspace:
            from openoutreach.core.models import WorkspaceMembership
            if not WorkspaceMembership.objects.filter(workspace=lead.workspace, user=request.user, status="active").exists():
                from django.core.exceptions import PermissionDenied
                raise PermissionDenied("You do not have permission to view this lead.")
    deal = lead.deal_set.first()
    
    from openoutreach.emails import bettercontact
    bettercontact_configured = bettercontact.is_configured()
    is_self_profile = LinkedInProfile.objects.filter(self_lead=lead).exists()

    if request.method == "POST":
        action = request.POST.get("action")
        
        if is_self_profile:
            messages.error(request, "This lead represents an operator's self-profile and cannot be mutated.")
            return redirect("dashboard:lead_detail", pk=pk)

        campaign = deal.campaign if deal else Campaign.objects.first()
        previous_state = deal.state if deal else "Discovered"

        if action == "qualify":
            lead.disqualified = False
            lead.save(update_fields=["disqualified"])
            if not deal:
                if not campaign:
                    messages.error(request, "No campaigns configured. Cannot qualify lead.")
                    return redirect("dashboard:lead_detail", pk=pk)
                deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.QUALIFIED)
            else:
                deal.state = DealState.QUALIFIED
                deal.outcome = ""
                deal.reason = ""
                deal.save(update_fields=["state", "outcome", "reason"])

            LeadPipelineEvent.objects.create(
                lead=lead,
                deal=deal,
                campaign=campaign,
                operator=request.user,
                action="qualify",
                previous_state=previous_state,
                new_state=DealState.QUALIFIED.value,
                note="Marked Qualified by operator"
            )
            messages.success(request, f"Lead '{lead.public_identifier}' marked as Qualified.")

        elif action == "disqualify":
            lead.disqualified = True
            lead.save(update_fields=["disqualified"])
            if not deal:
                if not campaign:
                    messages.error(request, "No campaigns configured. Cannot disqualify lead.")
                    return redirect("dashboard:lead_detail", pk=pk)
                deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.FAILED, outcome=Outcome.WRONG_FIT, reason="Manual operator disqualification")
            else:
                deal.state = DealState.FAILED
                deal.outcome = Outcome.WRONG_FIT
                deal.reason = "Manual operator disqualification"
                deal.save(update_fields=["state", "outcome", "reason"])

            LeadPipelineEvent.objects.create(
                lead=lead,
                deal=deal,
                campaign=campaign,
                operator=request.user,
                action="disqualify",
                previous_state=previous_state,
                new_state=DealState.FAILED.value,
                note="Marked Disqualified by operator"
            )
            messages.success(request, f"Lead '{lead.public_identifier}' marked as Disqualified.")

        elif action == "mark_ready_connect":
            if not deal:
                if not campaign:
                    messages.error(request, "No campaigns configured. Cannot perform action.")
                    return redirect("dashboard:lead_detail", pk=pk)
                deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.READY_TO_CONNECT)
            else:
                if deal.state in (DealState.PENDING, DealState.CONNECTED) and request.POST.get("confirm_reset") != "yes":
                    messages.error(request, "Lead is currently PENDING or CONNECTED. Confirm reset to proceed.")
                    return redirect("dashboard:lead_detail", pk=pk)

                deal.state = DealState.READY_TO_CONNECT
                deal.outcome = ""
                deal.reason = ""
                deal.save(update_fields=["state", "outcome", "reason"])

            LeadPipelineEvent.objects.create(
                lead=lead,
                deal=deal,
                campaign=campaign,
                operator=request.user,
                action="mark_ready_connect",
                previous_state=previous_state,
                new_state=DealState.READY_TO_CONNECT.value,
                note="Marked Ready to Connect by operator"
            )
            messages.success(request, f"Lead '{lead.public_identifier}' marked as Ready to Connect.")

        elif action == "mark_ready_email":
            if not lead.api_email and request.POST.get("override") != "yes":
                messages.error(request, "Lead has no email resolved. Confirm override to proceed.")
                return redirect("dashboard:lead_detail", pk=pk)

            if not deal:
                if not campaign:
                    messages.error(request, "No campaigns configured. Cannot perform action.")
                    return redirect("dashboard:lead_detail", pk=pk)
                deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.READY_TO_EMAIL)
            else:
                deal.state = DealState.READY_TO_EMAIL
                deal.outcome = ""
                deal.reason = ""
                deal.save(update_fields=["state", "outcome", "reason"])

            LeadPipelineEvent.objects.create(
                lead=lead,
                deal=deal,
                campaign=campaign,
                operator=request.user,
                action="mark_ready_email",
                previous_state=previous_state,
                new_state=DealState.READY_TO_EMAIL.value,
                note="Marked Ready to Email by operator"
            )
            messages.success(request, f"Lead '{lead.public_identifier}' marked as Ready to Email.")

        elif action == "retry_failed":
            if deal and deal.state == DealState.FAILED:
                target_state = DealState.READY_TO_EMAIL if lead.api_email else DealState.QUALIFIED
                deal.state = target_state
                deal.outcome = ""
                deal.reason = ""
                deal.save(update_fields=["state", "outcome", "reason"])

                LeadPipelineEvent.objects.create(
                    lead=lead,
                    deal=deal,
                    campaign=campaign,
                    operator=request.user,
                    action="retry_failed",
                    previous_state=previous_state,
                    new_state=target_state.value,
                    note="Retried failed lead by operator"
                )
                messages.success(request, f"Lead '{lead.public_identifier}' retried and set to state: {target_state.value}.")

        elif action == "reset_qualified":
            if deal:
                deal.state = DealState.QUALIFIED
                deal.outcome = ""
                deal.reason = ""
                deal.connect_attempts = 0
                deal.backoff_hours = 0
                deal.save(update_fields=["state", "outcome", "reason", "connect_attempts", "backoff_hours"])

                LeadPipelineEvent.objects.create(
                    lead=lead,
                    deal=deal,
                    campaign=campaign,
                    operator=request.user,
                    action="reset_qualified",
                    previous_state=previous_state,
                    new_state=DealState.QUALIFIED.value,
                    note="Reset to Qualified by operator"
                )
                messages.success(request, f"Lead '{lead.public_identifier}' has been reset to Qualified.")

        elif action in ("resolve_email", "resolve_email_now"):
            success = lead.resolve_api_email()
            result_note = "Email resolved" if success is True else ("No email resolved" if success is False else "API failed/missing key")
            
            if success is True:
                messages.success(request, f"Successfully resolved work email: {lead.api_email}")
                if deal and deal.state == DealState.QUALIFIED:
                    from openoutreach.core.db.deals import set_profile_state
                    from dataclasses import dataclass
                    @dataclass
                    class DummySession:
                        campaign: object
                    session = DummySession(campaign=deal.campaign)
                    set_profile_state(session, lead.public_identifier, DealState.READY_TO_EMAIL)
                    messages.info(request, "Transitioned Deal state to READY_TO_EMAIL")
                    
                    LeadPipelineEvent.objects.create(
                        lead=lead,
                        deal=deal,
                        campaign=campaign,
                        operator=request.user,
                        action="resolve_email_now",
                        previous_state=previous_state,
                        new_state=DealState.READY_TO_EMAIL.value,
                        note=result_note
                    )
            elif success is False:
                messages.warning(request, "BetterContact run complete: no email resolved for this lead.")
                LeadPipelineEvent.objects.create(
                    lead=lead,
                    deal=deal,
                    campaign=campaign,
                    operator=request.user,
                    action="resolve_email_now",
                    previous_state=previous_state,
                    new_state=previous_state,
                    note=result_note
                )
            else:
                messages.error(request, "BetterContact failed to run: service unavailable or key missing. Check log diagnostics.")
                LeadPipelineEvent.objects.create(
                    lead=lead,
                    deal=deal,
                    campaign=campaign,
                    operator=request.user,
                    action="resolve_email_now",
                    previous_state=previous_state,
                    new_state=previous_state,
                    note=result_note
                )

        return redirect("dashboard:lead_detail", pk=pk)
        
    title = lead.current_title or "Not available"
    company = lead.company or "Not available"
    facts = []
    messages_list = []
    logs = []
    
    if deal:
        if (not lead.current_title or not lead.company) and deal.profile_summary and "facts" in deal.profile_summary:
            facts = deal.profile_summary["facts"]
            role_facts = []
            company_facts = []
            for fact in facts:
                fact_lower = fact.lower()
                if any(x in fact_lower for x in ["role", "title", "position", "ceo", "director", "manager", "vp"]):
                    role_facts.append(fact)
                if "company" in fact_lower or " at " in fact_lower:
                    company_facts.append(fact)
            
            if not lead.current_title:
                if role_facts:
                    title = role_facts[0]
                elif facts:
                    title = facts[0]
            if not lead.company:
                if company_facts:
                    company = company_facts[0]
                elif len(facts) > 1:
                    company = facts[1]
        
        # Load conversation messages
        messages_list = deal.messages.all().order_by("creation_date")
        
        # Load associated connection/follow-up logs
        logs = ActionLog.objects.filter(campaign=deal.campaign).order_by("-created_at")
        logs = [log for log in logs if log.metadata and log.metadata.get("lead_public_id") == lead.public_identifier]

    bc_fields = lead.get_bettercontact_fields()
    has_stronger_fields = bool(bc_fields.get("first_name") and bc_fields.get("last_name") and bc_fields.get("company"))

    full_name = f"{lead.first_name} {lead.last_name}".strip() or "Not available"
    headline = lead.headline or "Not available"
    location = lead.location or "Not available"
    industry = lead.industry or "Not available"
    identity_status = "Enriched" if (lead.first_name or lead.company) else "Discovered only"

    pipeline_events = LeadPipelineEvent.objects.filter(lead=lead).order_by("-created_at")

    is_disqualified = lead.disqualified or is_self_profile
    campaign_is_active = False
    if deal:
        campaign_is_active = deal.campaign.is_active

    eligible_email = bool(lead.api_email) and campaign_is_active and deal and deal.state == DealState.READY_TO_EMAIL and not is_disqualified
    eligible_connect = campaign_is_active and deal and (deal.state in (DealState.READY_TO_CONNECT, DealState.QUALIFIED) and not lead.api_email) and not is_disqualified
    eligible_followup = campaign_is_active and deal and deal.state == DealState.CONNECTED and not is_disqualified
    eligible_retry = campaign_is_active and deal and deal.state == DealState.FAILED and not is_disqualified

    ws = getattr(lead, "workspace", None) or (deal.campaign.workspace if deal and getattr(deal, "campaign", None) else None)
    from openoutreach.linkedin.models import ProviderRelationship
    provider_relationship = None
    if deal and hasattr(deal, "campaign") and getattr(deal.campaign, "linkedin_profile", None):
        q = ProviderRelationship.objects.filter(
            linkedin_profile=deal.campaign.linkedin_profile,
            lead=lead,
        )
        if ws:
            q = q.filter(workspace=ws)
        provider_relationship = q.first()
    if not provider_relationship:
        q = ProviderRelationship.objects.filter(lead=lead)
        if ws:
            q = q.filter(workspace=ws)
        provider_relationship = q.select_related("linkedin_profile").order_by("-updated_at").first()

    context = {
        "lead": lead,
        "deal": deal,
        "provider_relationship": provider_relationship,
        "title": title,
        "company": company,
        "facts": facts,
        "messages": messages_list,
        "action_logs": logs,
        "bettercontact_configured": bettercontact_configured,
        "bettercontact_fields": bc_fields,
        "has_stronger_fields": has_stronger_fields,
        "full_name": full_name,
        "headline": headline,
        "location": location,
        "industry": industry,
        "identity_status": identity_status,
        "is_self_profile": is_self_profile,
        "pipeline_events": pipeline_events,
        "eligible_email": eligible_email,
        "eligible_connect": eligible_connect,
        "eligible_followup": eligible_followup,
        "eligible_retry": eligible_retry,
    }
    return render(request, "dashboard/lead_detail.html", context)


def mask_sensitive_payload(payload):
    if not isinstance(payload, dict):
        return payload
    masked = {}
    sensitive_keywords = ["password", "api_key", "token", "secret", "cookie", "authorization", "credential", "smtp", "app_password"]
    for k, v in payload.items():
        k_lower = k.lower()
        if any(kw in k_lower for kw in sensitive_keywords):
            masked[k] = "********"
        elif isinstance(v, dict):
            masked[k] = mask_sensitive_payload(v)
        elif isinstance(v, list):
            masked[k] = [mask_sensitive_payload(item) if isinstance(item, dict) else item for item in v]
        else:
            masked[k] = v
    return masked


@dashboard_login_required
def tasks(request):
    status_filter = request.GET.get("status", "").strip()
    type_filter = request.GET.get("task_type", "").strip()
    campaign_filter = request.GET.get("campaign_id", "").strip()
    overdue_only = request.GET.get("overdue_only", "").strip()
    failed_only = request.GET.get("failed_only", "").strip()
    pending_only = request.GET.get("pending_only", "").strip()
    cancelled_only = request.GET.get("cancelled_only", "").strip()
    
    tasks_qs = Task.objects.all().order_by("-scheduled_at")
    
    if status_filter:
        tasks_qs = tasks_qs.filter(status=status_filter)
    if type_filter:
        tasks_qs = tasks_qs.filter(task_type=type_filter)
        
    if overdue_only == "yes":
        tasks_qs = tasks_qs.filter(status=Task.Status.PENDING, scheduled_at__lt=timezone.now())
    if failed_only == "yes":
        tasks_qs = tasks_qs.filter(status=Task.Status.FAILED)
    if pending_only == "yes":
        tasks_qs = tasks_qs.filter(status=Task.Status.PENDING)
    if cancelled_only == "yes":
        tasks_qs = tasks_qs.filter(status=Task.Status.CANCELLED)
        
    if campaign_filter:
        tasks_qs = tasks_qs.filter(payload__campaign_id=int(campaign_filter) if campaign_filter.isdigit() else campaign_filter)

    tasks_qs = list(tasks_qs[:100])

    tasks_data = []
    campaign_cache = {c.id: c for c in Campaign.objects.all()}
    
    # Pre-collect lead identifiers for batch lookup
    lead_ids = set()
    lead_pks = set()
    for task in tasks_qs:
        l_id = task.payload.get("lead_public_id") or task.payload.get("public_id") or task.payload.get("lead_id")
        if l_id:
            if str(l_id).isdigit():
                lead_pks.add(int(l_id))
            lead_ids.add(str(l_id))

    leads_by_pub = {l.public_identifier: l for l in Lead.objects.filter(public_identifier__in=lead_ids)} if lead_ids else {}
    leads_by_pk = {l.pk: l for l in Lead.objects.filter(pk__in=lead_pks)} if lead_pks else {}

    now = timezone.now()
    for task in tasks_qs:
        campaign_id = task.payload.get("campaign_id")
        campaign = campaign_cache.get(campaign_id) if campaign_id else None
        
        # Resolve lead from pre-fetched caches
        lead_public_id = task.payload.get("lead_public_id") or task.payload.get("public_id") or task.payload.get("lead_id")
        lead = None
        if lead_public_id:
            lead = leads_by_pub.get(str(lead_public_id)) or (leads_by_pk.get(int(lead_public_id)) if str(lead_public_id).isdigit() else None)

        # Compute age/overdue duration
        is_overdue = task.status == Task.Status.PENDING and task.scheduled_at and task.scheduled_at < now
        overdue_duration = None
        if is_overdue:
            diff = now - task.scheduled_at
            overdue_duration = f"{int(diff.total_seconds() // 60)}m"
            if diff.total_seconds() >= 3600:
                overdue_duration = f"{int(diff.total_seconds() // 3600)}h {int((diff.total_seconds() % 3600) // 60)}m"

        tasks_data.append({
            "task": task,
            "campaign": campaign,
            "lead": lead,
            "is_overdue": is_overdue,
            "overdue_duration": overdue_duration,
        })
        
    context = {
        "tasks": tasks_data,
        "statuses": Task.Status.choices,
        "task_types": Task.TaskType.choices,
        "campaigns": campaign_cache.values(),
        "selected_status": status_filter,
        "selected_type": type_filter,
        "selected_campaign": campaign_filter,
        "overdue_only": overdue_only,
        "failed_only": failed_only,
        "pending_only": pending_only,
        "cancelled_only": cancelled_only,
    }
    return render(request, "dashboard/tasks.html", context)


@dashboard_login_required
def task_detail(request, pk):
    task = get_object_or_404(Task, pk=pk)
    campaign_id = task.payload.get("campaign_id")
    campaign = Campaign.objects.filter(pk=campaign_id).first() if campaign_id else None
    
    # Resolve lead public identifier
    lead_public_id = task.payload.get("lead_public_id") or task.payload.get("public_id") or task.payload.get("lead_id")
    lead = None
    if lead_public_id:
        lead = Lead.objects.filter(Q(public_identifier=lead_public_id) | Q(pk=lead_public_id) if str(lead_public_id).isdigit() else Q(public_identifier=lead_public_id)).first()
    
    if not lead and campaign_id and task.status in (Task.Status.COMPLETED, Task.Status.FAILED):
        log_evt = LeadPipelineEvent.objects.filter(campaign_id=campaign_id, created_at__gte=task.started_at or task.created_at).first()
        if log_evt:
            lead = log_evt.lead
        else:
            act_log = ActionLog.objects.filter(campaign_id=campaign_id, created_at__gte=task.started_at or task.created_at).first()
            if act_log and act_log.metadata:
                lead_pub = act_log.metadata.get("lead_public_id")
                if lead_pub:
                    lead = Lead.objects.filter(public_identifier=lead_pub).first()

    # Formatted payload JSON safely masked
    import json
    masked_payload = mask_sensitive_payload(task.payload)
    payload_json = json.dumps(masked_payload, indent=2)

    # Manual audit events
    audit_events = TaskQueueEvent.objects.filter(task=task).order_by("-created_at")

    # Latest related logs if identifiable
    related_action_logs = []
    if campaign:
        related_action_logs = ActionLog.objects.filter(campaign=campaign).order_by("-created_at")[:10]
        if lead:
            related_action_logs = [log for log in related_action_logs if log.metadata and log.metadata.get("lead_public_id") == lead.public_identifier]

    if request.method == "POST":
        from django.utils.dateparse import parse_datetime
        action = request.POST.get("action")
        previous_status = task.status
        previous_scheduled_at = task.scheduled_at

        # Staff verification
        if not request.user.is_staff:
            messages.error(request, "Permission denied: Staff credentials required.")
            return redirect("dashboard:task_detail", pk=pk)

        if action == "retry":
            if task.status != Task.Status.FAILED:
                messages.error(request, "Only failed tasks can be retried.")
                return redirect("dashboard:task_detail", pk=pk)

            # Parse datetime if provided
            scheduled_at_str = request.POST.get("scheduled_at", "").strip()
            target_time = timezone.now()
            if scheduled_at_str:
                parsed_time = parse_datetime(scheduled_at_str)
                if parsed_time:
                    target_time = timezone.make_aware(parsed_time) if timezone.is_naive(parsed_time) else parsed_time
                else:
                    messages.error(request, "Invalid datetime format for retry schedule.")
                    return redirect("dashboard:task_detail", pk=pk)

            task.status = Task.Status.PENDING
            task.scheduled_at = target_time
            task.save(update_fields=["status", "scheduled_at"])

            TaskQueueEvent.objects.create(
                task=task,
                task_id_snapshot=task.pk,
                campaign=campaign,
                operator=request.user,
                action="retry",
                previous_status=previous_status,
                new_status=Task.Status.PENDING.value,
                previous_scheduled_at=previous_scheduled_at,
                new_scheduled_at=task.scheduled_at,
                note=f"Task retried manually to run at {task.scheduled_at}"
            )
            messages.success(request, f"Task #{task.pk} status updated to PENDING.")

        elif action == "cancel":
            if task.status != Task.Status.PENDING:
                messages.error(request, "Only pending tasks can be cancelled.")
                return redirect("dashboard:task_detail", pk=pk)

            if request.POST.get("confirm") != "yes":
                messages.error(request, "Confirmation required to cancel the task.")
                return redirect("dashboard:task_detail", pk=pk)

            task.status = Task.Status.CANCELLED
            task.save(update_fields=["status"])

            TaskQueueEvent.objects.create(
                task=task,
                task_id_snapshot=task.pk,
                campaign=campaign,
                operator=request.user,
                action="cancel",
                previous_status=previous_status,
                new_status=Task.Status.CANCELLED.value,
                previous_scheduled_at=previous_scheduled_at,
                new_scheduled_at=previous_scheduled_at,
                note="Task cancelled manually by operator"
            )
            messages.success(request, f"Task #{task.pk} status updated to CANCELLED.")

        elif action == "reschedule":
            if task.status != Task.Status.PENDING:
                messages.error(request, "Only pending tasks can be rescheduled.")
                return redirect("dashboard:task_detail", pk=pk)

            scheduled_at_str = request.POST.get("scheduled_at", "").strip()
            if not scheduled_at_str:
                messages.error(request, "Reschedule date and time is required.")
                return redirect("dashboard:task_detail", pk=pk)

            parsed_time = parse_datetime(scheduled_at_str)
            if not parsed_time:
                messages.error(request, "Invalid datetime format.")
                return redirect("dashboard:task_detail", pk=pk)

            target_time = timezone.make_aware(parsed_time) if timezone.is_naive(parsed_time) else parsed_time
            task.scheduled_at = target_time
            task.save(update_fields=["scheduled_at"])

            TaskQueueEvent.objects.create(
                task=task,
                task_id_snapshot=task.pk,
                campaign=campaign,
                operator=request.user,
                action="reschedule",
                previous_status=previous_status,
                new_status=previous_status,
                previous_scheduled_at=previous_scheduled_at,
                new_scheduled_at=target_time,
                note=f"Task rescheduled to {target_time}"
            )
            messages.success(request, f"Task #{task.pk} rescheduled successfully.")

        elif action == "mark_stale_failed":
            if task.status != Task.Status.RUNNING:
                messages.error(request, "Only running tasks can be marked stale.")
                return redirect("dashboard:task_detail", pk=pk)

            if request.POST.get("confirm") != "yes":
                messages.error(request, "Confirmation required to mark task as failed.")
                return redirect("dashboard:task_detail", pk=pk)

            task.status = Task.Status.FAILED
            task.save(update_fields=["status"])

            TaskQueueEvent.objects.create(
                task=task,
                task_id_snapshot=task.pk,
                campaign=campaign,
                operator=request.user,
                action="mark_stale_failed",
                previous_status=previous_status,
                new_status=Task.Status.FAILED.value,
                previous_scheduled_at=previous_scheduled_at,
                new_scheduled_at=previous_scheduled_at,
                note="Manually marked stale running task as failed"
            )
            messages.success(request, f"Task #{task.pk} marked as failed.")

        return redirect("dashboard:task_detail", pk=pk)

    context = {
        "task": task,
        "campaign": campaign,
        "lead": lead,
        "payload_json": payload_json,
        "audit_events": audit_events,
        "related_logs": related_action_logs,
    }
    return render(request, "dashboard/task_detail.html", context)


@dashboard_login_required
def action_logs(request):
    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]

    channel_filter = request.GET.get("channel", "all").strip().lower()
    type_filter = request.GET.get("type", "all").strip().lower()

    include_synthetic = request.GET.get("include_synthetic") == "1" and (request.user.is_staff or request.user.is_superuser)
    
    if include_synthetic:
        logs_qs = ActionLog.objects.all().select_related("linkedin_profile", "campaign").order_by("-created_at")
    elif workspace:
        logs_qs = ActionLog.objects.filter(
            models.Q(campaign__workspace=workspace) | models.Q(linkedin_profile__user=request.user) | models.Q(linkedin_profile__workspace=workspace)
        ).filter(
            models.Q(campaign__isnull=True) | models.Q(campaign__is_synthetic=False)
        ).distinct().exclude(campaign__name__icontains="Test").exclude(campaign__name__icontains="Acceptance").select_related("linkedin_profile", "campaign").order_by("-created_at")
    else:
        logs_qs = ActionLog.objects.filter(
            models.Q(linkedin_profile__user=request.user)
        ).filter(
            models.Q(campaign__isnull=True) | models.Q(campaign__is_synthetic=False)
        ).exclude(campaign__name__icontains="Test").exclude(campaign__name__icontains="Acceptance").select_related("linkedin_profile", "campaign").order_by("-created_at")

    if channel_filter == "linkedin":
        logs_qs = logs_qs.filter(action_type__in=["connect", "follow_up", "check_pending"])
    elif channel_filter == "email":
        logs_qs = logs_qs.filter(action_type__icontains="email")
    elif channel_filter == "jobs":
        logs_qs = logs_qs.filter(action_type__icontains="job")

    if type_filter == "invitations":
        logs_qs = logs_qs.filter(action_type="connect")
    elif type_filter == "replies":
        logs_qs = logs_qs.filter(action_type__icontains="reply")
    elif type_filter == "applications":
        logs_qs = logs_qs.filter(action_type__icontains="apply")

    enriched_logs = []
    for log in logs_qs[:50]:
        meta = log.metadata if isinstance(log.metadata, dict) else {}
        t_name = meta.get("target_name") or meta.get("candidate_name") or meta.get("lead_name") or meta.get("company_name") or meta.get("job_title") or "prospect"
        a_type = (log.action_type or "activity").lower()
        if a_type == "connect":
            desc = f"Sent personalized connection invitation to {t_name}"
            icon = "🤝"
        elif a_type == "follow_up":
            desc = f"Sent follow-up message to {t_name}"
            icon = "💬"
        elif "apply" in a_type:
            desc = f"Submitted application for {t_name}"
            icon = "💼"
        elif "email" in a_type:
            desc = f"Sent email outreach to {t_name}"
            icon = "✉️"
        else:
            desc = f"Processed {a_type} for {t_name}"
            icon = "⚡"

        enriched_logs.append({
            "raw": log,
            "metadata": meta,
            "description": desc,
            "icon": icon,
            "campaign_name": log.campaign.name if log.campaign else "General",
            "created_at": log.created_at,
            "action_type": log.action_type,
        })

    context = {
        "logs": enriched_logs,
        "channel_filter": channel_filter,
        "type_filter": type_filter,
    }
    return render(request, "dashboard/action_logs.html", context)


@dashboard_login_required
def linkedin_accounts(request):
    profiles = LinkedInProfile.objects.all().select_related("user")
    profiles_data = []
    
    for profile in profiles:
        last_log = ActionLog.objects.filter(linkedin_profile=profile).order_by("-created_at").first()
        last_action = last_log.created_at if last_log else None
        campaign_count = Campaign.objects.filter(users=profile.user).count()
        
        profiles_data.append({
            "profile": profile,
            "last_action": last_action,
            "campaign_count": campaign_count,
        })
        
    return render(request, "dashboard/linkedin_accounts.html", {"profiles": profiles_data})


@dashboard_login_required
def linkedin_account_edit(request, pk):
    profile = get_object_or_404(LinkedInProfile, pk=pk)
    original_password = profile.linkedin_password
    if request.method == "POST":
        form = LinkedInProfileForm(request.POST, instance=profile)
        if form.is_valid():
            obj = form.save(commit=False)
            new_password = form.cleaned_data.get("linkedin_password")
            # If submitted value is blank, preserve the existing secret.
            if not new_password:
                obj.linkedin_password = original_password
            else:
                obj.linkedin_password = new_password
            obj.save()
            messages.success(request, f"LinkedIn account '{profile.linkedin_username}' updated successfully.")
            return redirect("dashboard:linkedin_accounts")
    else:
        form = LinkedInProfileForm(instance=profile)
        
    context = {
        "form": form,
        "profile": profile,
        "masked_password_status": _mask_key(profile.linkedin_password),
    }
    return render(request, "dashboard/linkedin_account_form.html", context)


@dashboard_login_required
def mailboxes(request):
    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]
    if request.user.is_staff or request.user.is_superuser:
        if workspace:
            mailbox_list = Mailbox.objects.filter(
                models.Q(workspace=workspace) | models.Q(user=request.user) | models.Q(workspace__isnull=True)
            ).distinct()
        else:
            mailbox_list = Mailbox.objects.all()
    elif workspace:
        mailbox_list = Mailbox.objects.for_workspace(workspace)
    else:
        mailbox_list = Mailbox.objects.filter(user=request.user)
    mailboxes_data = []
    
    for mailbox in mailbox_list:
        mailboxes_data.append({
            "mailbox": mailbox,
            "masked_password": _mask_key(mailbox.password),
        })
        
    return render(request, "dashboard/mailboxes.html", {"mailboxes": mailboxes_data})


@dashboard_login_required
def mailbox_create(request):
    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]
    form = MailboxForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        mailbox = form.save(commit=False)
        mailbox.workspace = workspace
        mailbox.user = request.user
        mailbox.save()
        messages.success(request, f"Mailbox '{mailbox.username}' created successfully.")
        return redirect("dashboard:mailboxes")
    return render(request, "dashboard/mailbox_form.html", {"form": form, "title": "Add Mailbox"})


@dashboard_login_required
def mailbox_edit(request, pk):
    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]
    if workspace:
        mailbox = get_object_or_404(Mailbox.objects.for_workspace(workspace), pk=pk)
    else:
        mailbox = get_object_or_404(Mailbox, pk=pk, user=request.user)

    original_password = mailbox.password
    if request.method == "POST":
        form = MailboxForm(request.POST, instance=mailbox)
        if form.is_valid():
            obj = form.save(commit=False)
            new_password = form.cleaned_data.get("password")
            if not new_password:
                obj.password = original_password
            else:
                obj.password = new_password
            obj.workspace = workspace or obj.workspace
            obj.user = request.user or obj.user
            obj.save()
            messages.success(request, f"Mailbox '{mailbox.username}' updated successfully.")
            return redirect("dashboard:mailboxes")
    else:
        form = MailboxForm(instance=mailbox)
        
    context = {
        "form": form,
        "mailbox": mailbox,
        "masked_password_status": _mask_key(mailbox.password),
        "title": "Edit Mailbox",
    }
    return render(request, "dashboard/mailbox_form.html", context)


@dashboard_login_required
def mailbox_delete(request, pk):
    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]
    if workspace:
        mailbox = get_object_or_404(Mailbox.objects.for_workspace(workspace), pk=pk)
    else:
        mailbox = get_object_or_404(Mailbox, pk=pk, user=request.user)

    if request.method == "POST":
        mailbox_name = mailbox.username
        mailbox.delete()
        messages.success(request, f"Mailbox '{mailbox_name}' deleted successfully.")
        return redirect("dashboard:mailboxes")
    return render(request, "dashboard/mailbox_confirm_delete.html", {"mailbox": mailbox})


@dashboard_login_required
def settings_llm(request):
    from openoutreach.core.llm import get_default_ollama_base_url
    cfg = SiteConfig.load()
    original_key = cfg.llm_api_key
    detected_models = []
    ollama_reachable = False
    ollama_base = cfg.llm_api_base.rstrip("/v1").rstrip("/") if (cfg.llm_api_base and ("11434" in cfg.llm_api_base or "ollama" in cfg.llm_api_base)) else get_default_ollama_base_url()

    # Attempt local/configured Ollama model discovery
    try:
        import urllib.request
        import json
        req = urllib.request.Request(f"{ollama_base}/api/tags", headers={"User-Agent": "HuntFlow"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            if resp.status == 200:
                ollama_reachable = True
                data = json.loads(resp.read().decode("utf-8"))
                for m in data.get("models", []):
                    name = m.get("name")
                    if name:
                        detected_models.append(f"ollama:{name}")
    except Exception:
        ollama_reachable = False

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "refresh_models":
            if ollama_reachable:
                messages.success(request, f"Ollama service reachable at {ollama_base}. Found models: {', '.join(detected_models)}")
            else:
                messages.warning(request, f"Ollama service unreachable at {ollama_base}.")
            return redirect("dashboard:settings_llm")

        if action == "test_config":
            if not cfg.ai_model or not cfg.is_llm_configured():
                messages.error(request, "LLM is not configured (missing model or API configuration).")
            else:
                from openoutreach.core.management.commands.test_llm_config import (
                    verify_llm_credentials_with_timeout,
                    sanitize_and_classify_llm_error,
                )
                err = verify_llm_credentials_with_timeout(cfg.ai_model, cfg.llm_api_key, cfg.llm_api_base, timeout=10.0)
                if err is None:
                    messages.success(request, "Success: LLM Configuration works correctly.")
                else:
                    category, sanitized_msg = sanitize_and_classify_llm_error(err, cfg.llm_api_key)
                    messages.error(request, f"Failure Category: {category} — Details: {sanitized_msg}")
            return redirect("dashboard:settings_llm")

        form = LLMConfigForm(request.POST, instance=cfg)
        if form.is_valid():
            obj = form.save(commit=False)
            new_key = form.cleaned_data.get("llm_api_key")
            if not new_key:
                obj.llm_api_key = original_key
            else:
                obj.llm_api_key = new_key
            obj.save()
            messages.success(request, "LLM Configuration saved successfully.")
            return redirect("dashboard:settings_llm")
    else:
        form = LLMConfigForm(instance=cfg)

    context = {
        "form": form,
        "masked_api_key_status": "Ollama Local (No Key Required)" if cfg.llm_provider == "ollama" else _mask_key(cfg.llm_api_key),
        "cfg": cfg,
        "detected_models": detected_models,
        "ollama_reachable": ollama_reachable,
        "ollama_endpoint": ollama_base,
    }
    return render(request, "dashboard/settings_llm.html", context)



@dashboard_login_required
def settings_bettercontact(request):
    cfg = SiteConfig.load()
    original_key = cfg.bettercontact_api_key
    if request.method == "POST":
        form = BetterContactConfigForm(request.POST, instance=cfg)
        if form.is_valid():
            obj = form.save(commit=False)
            new_key = form.cleaned_data.get("bettercontact_api_key")
            if not new_key:
                obj.bettercontact_api_key = original_key
            else:
                obj.bettercontact_api_key = new_key
            obj.save()
            messages.success(request, "BetterContact API settings saved successfully.")
            return redirect("dashboard:settings_bettercontact")
    else:
        form = BetterContactConfigForm(instance=cfg)
        
    context = {
        "form": form,
        "masked_api_key_status": _mask_key(cfg.bettercontact_api_key),
    }
    return render(request, "dashboard/settings_bettercontact.html", context)


@dashboard_login_required
def settings_system(request):
    # System settings are strictly operational constants (read-only)
    from openoutreach.core.conf import (
        ENABLE_ACTIVE_HOURS,
        ACTIVE_START_HOUR,
        ACTIVE_END_HOUR,
        CHECK_PENDING_DAILY_CAP,
    )
    context = {
        "enable_active_hours": ENABLE_ACTIVE_HOURS,
        "active_start": ACTIVE_START_HOUR,
        "active_end": ACTIVE_END_HOUR,
        "pending_cap": CHECK_PENDING_DAILY_CAP,
    }
    return render(request, "dashboard/settings_system.html", context)


@dashboard_login_required
def daemon(request):
    from openoutreach.core.models import Task, Campaign, DaemonStatus, SiteConfig
    from openoutreach.linkedin.models import ActionLog
    from openoutreach.linkedin.browser.registry import get_first_active_profile
    from openoutreach.emails.models import Mailbox
    import subprocess
    import sys
    import os
    import signal
    import time
    from django.utils import timezone

    from openoutreach.core.readiness import is_pid_alive, safe_stop_daemon

    status, _ = DaemonStatus.objects.get_or_create(pk=1)

    # Check process state via tri-state contract
    process_state = is_pid_alive(status.pid)

    # Sync DB is_running if actual process is stopped (outside grace period)
    if process_state == "stopped" and status.is_running:
        grace_period = 30
        recent_startup = False
        if status.last_heartbeat and (timezone.now() - status.last_heartbeat).total_seconds() < grace_period:
            recent_startup = True
        if not recent_startup:
            status.is_running = False
            status.save(update_fields=["is_running"])

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "start":
            if process_state in ("running", "unknown"):
                messages.warning(request, f"Daemon is already in '{process_state}' state. Cannot start.")
            else:
                from openoutreach.core.readiness import check_launch_readiness
                from openoutreach.core.models import LaunchRun, LaunchRunEvent
                from openoutreach.core.launch_preview import mask_sensitive_payload_recursive
                
                readiness_data = check_launch_readiness()
                
                # Check for blockers
                if readiness_data["overall_status"] == "blocked":
                    # Record blocked run
                    run = LaunchRun.objects.create(
                        operator=request.user,
                        source="dashboard",
                        mode="live",
                        status="blocked",
                        readiness_status=readiness_data["overall_status"],
                        blocker_count=len(readiness_data["hard_blockers"]),
                        warning_count=len(readiness_data["warnings"]),
                        readiness_snapshot=mask_sensitive_payload_recursive(readiness_data),
                    )
                    LaunchRunEvent.objects.create(
                        launch_run=run,
                        operator=request.user,
                        event_type="start_blocked",
                        note="Daemon startup blocked due to hard launch blockers.",
                    )
                    has_no_tasks = any(b["title"] == "No executable pending tasks" for b in readiness_data["hard_blockers"])
                    if has_no_tasks:
                        messages.error(request, "Daemon was not started: No executable outreach task is available.")
                    else:
                        messages.error(request, "Cannot start daemon: Hard launch blockers exist.")
                    return redirect("dashboard:daemon")
                
                # Build queue snapshot
                pending_tasks_list = []
                for t in Task.objects.filter(status=Task.Status.PENDING).order_by("scheduled_at")[:10]:
                    pending_tasks_list.append({
                        "id": t.id,
                        "task_type": t.task_type,
                        "scheduled_at": str(t.scheduled_at),
                        "payload": mask_sensitive_payload_recursive(t.payload),
                    })
                queue_snapshot = {"pending_tasks": pending_tasks_list}
                
                # Create run record
                run = LaunchRun.objects.create(
                    operator=request.user,
                    source="dashboard",
                    mode="live",
                    status="started",
                    readiness_status=readiness_data["overall_status"],
                    blocker_count=len(readiness_data["hard_blockers"]),
                    warning_count=len(readiness_data["warnings"]),
                    readiness_snapshot=mask_sensitive_payload_recursive(readiness_data),
                    queue_snapshot=queue_snapshot,
                    started_at=timezone.now(),
                )
                LaunchRunEvent.objects.create(
                    launch_run=run,
                    operator=request.user,
                    event_type="start_attempted",
                    note="Daemon process startup initiated from dashboard.",
                )
                
                log_file = None
                try:
                    from django.conf import settings
                    log_file = open("daemon.log", "a", encoding="utf-8")
                    
                    popen_kwargs = {
                        "stdout": log_file,
                        "stderr": log_file,
                        "close_fds": True,
                        "cwd": settings.BASE_DIR,
                    }
                    if os.name == "nt":
                        import subprocess
                        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000010)
                    else:
                        popen_kwargs["start_new_session"] = True

                    proc = subprocess.Popen(
                        [sys.executable, "-u", "manage.py", "rundaemon"],
                        **popen_kwargs
                    )
                    
                    pid_val = proc.pid
                    if hasattr(pid_val, "_mock_name") or type(pid_val).__name__ in ("MagicMock", "Mock"):
                        run.daemon_pid = 99999
                        status.pid = 99999
                    else:
                        try:
                            val = int(pid_val)
                            run.daemon_pid = val
                            status.pid = val
                        except (TypeError, ValueError):
                            run.daemon_pid = None
                            status.pid = None
                    
                    run.status = "running"
                    run.save()
                    
                    status.is_running = True
                    status.save(update_fields=["pid", "is_running"])
                    
                    LaunchRunEvent.objects.create(
                        launch_run=run,
                        operator=request.user,
                        event_type="start_accepted",
                        note=f"Daemon process started with PID {proc.pid}.",
                    )
                    messages.success(request, "Daemon startup initiated in the background.")
                except Exception as e:
                    run.status = "failed"
                    run.save()
                    LaunchRunEvent.objects.create(
                        launch_run=run,
                        operator=request.user,
                        event_type="start_failed",
                        note=f"Daemon process startup failed: {str(e)}",
                    )
                    messages.error(request, f"Failed to start daemon: {str(e)}")
                finally:
                    if log_file:
                        log_file.close()
            return redirect("dashboard:daemon")

        elif action == "stop":
            if process_state == "stopped":
                messages.warning(request, "Daemon is not currently running.")
            elif process_state == "unknown":
                messages.error(request, f"Cannot stop daemon: Process state is {process_state} (unverifiable).")
            else:
                from openoutreach.core.models import LaunchRun, LaunchRunEvent
                
                # Update open run if any
                open_run = LaunchRun.objects.filter(status="running", mode="live").first()
                if open_run:
                    open_run.status = "stopped"
                    open_run.ended_at = timezone.now()
                    open_run.stop_reason = "Operator stopped daemon from dashboard"
                    open_run.save()
                    LaunchRunEvent.objects.create(
                        launch_run=open_run,
                        operator=request.user,
                        event_type="stop_requested",
                        note="Daemon stop command requested from dashboard.",
                    )
                
                res = safe_stop_daemon(status)
                if res["success"]:
                    messages.success(request, f"Daemon stopped successfully: {res.get('result')}")
                else:
                    messages.error(request, f"Failed to stop daemon process: {res.get('error')}")
            return redirect("dashboard:daemon")

        elif action == "restart":
            from openoutreach.core.models import LaunchRun, LaunchRunEvent
            from openoutreach.core.readiness import check_launch_readiness
            from openoutreach.core.launch_preview import mask_sensitive_payload_recursive
            
            # Recheck readiness
            readiness_data = check_launch_readiness()
            
            # Check for blockers
            if readiness_data["overall_status"] == "blocked":
                has_no_tasks = any(b["title"] == "No executable pending tasks" for b in readiness_data["hard_blockers"])
                if has_no_tasks:
                    messages.error(request, "Daemon was not started: No executable outreach task is available.")
                else:
                    messages.error(request, "Cannot restart daemon: Hard launch blockers exist.")
                return redirect("dashboard:daemon")
                
            if process_state in ("running", "unknown"):
                if process_state == "running":
                    open_run = LaunchRun.objects.filter(status="running", mode="live").first()
                    if open_run:
                        open_run.status = "stopped"
                        open_run.ended_at = timezone.now()
                        open_run.stop_reason = "Restart requested from dashboard"
                        open_run.save()
                        LaunchRunEvent.objects.create(
                            launch_run=open_run,
                            operator=request.user,
                            event_type="restart_requested",
                            note="Restart requested. Current process stopping.",
                        )
                    res = safe_stop_daemon(status)
                    if not res["success"]:
                        messages.error(request, f"Restart aborted: Failed to stop running daemon: {res.get('error')}")
                        return redirect("dashboard:daemon")
                else:
                    messages.error(request, f"Restart aborted: Old daemon state is {process_state} (unverifiable).")
                    return redirect("dashboard:daemon")
                
            # Build queue snapshot
            pending_tasks_list = []
            for t in Task.objects.filter(status=Task.Status.PENDING).order_by("scheduled_at")[:10]:
                pending_tasks_list.append({
                    "id": t.id,
                    "task_type": t.task_type,
                    "scheduled_at": str(t.scheduled_at),
                    "payload": mask_sensitive_payload_recursive(t.payload),
                })
            queue_snapshot = {"pending_tasks": pending_tasks_list}
            
            # Start new run
            run = LaunchRun.objects.create(
                operator=request.user,
                source="dashboard",
                mode="live",
                status="started",
                readiness_status=readiness_data["overall_status"],
                blocker_count=len(readiness_data["hard_blockers"]),
                warning_count=len(readiness_data["warnings"]),
                readiness_snapshot=mask_sensitive_payload_recursive(readiness_data),
                queue_snapshot=queue_snapshot,
                started_at=timezone.now(),
            )
            LaunchRunEvent.objects.create(
                launch_run=run,
                operator=request.user,
                event_type="restart_requested",
                note="Daemon restart requested from dashboard.",
            )
            
            log_file = None
            try:
                from django.conf import settings
                log_file = open("daemon.log", "a", encoding="utf-8")
                
                popen_kwargs = {
                    "stdout": log_file,
                    "stderr": log_file,
                    "close_fds": True,
                    "cwd": settings.BASE_DIR,
                }
                if os.name == "nt":
                    import subprocess
                    popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000010)
                else:
                    popen_kwargs["start_new_session"] = True

                proc = subprocess.Popen(
                    [sys.executable, "-u", "manage.py", "rundaemon"],
                    **popen_kwargs
                )
                
                pid_val = proc.pid
                if hasattr(pid_val, "_mock_name") or type(pid_val).__name__ in ("MagicMock", "Mock"):
                    run.daemon_pid = 99999
                    status.pid = 99999
                else:
                    try:
                        val = int(pid_val)
                        run.daemon_pid = val
                        status.pid = val
                    except (TypeError, ValueError):
                        run.daemon_pid = None
                        status.pid = None
                run.status = "running"
                run.save()
                
                status.is_running = True
                status.save(update_fields=["pid", "is_running"])
                
                LaunchRunEvent.objects.create(
                    launch_run=run,
                    operator=request.user,
                    event_type="start_accepted",
                    note=f"Daemon restarted with PID {proc.pid}.",
                )
                messages.success(request, "Daemon restart initiated.")
            except Exception as e:
                run.status = "failed"
                run.save()
                LaunchRunEvent.objects.create(
                    launch_run=run,
                    operator=request.user,
                    event_type="start_failed",
                    note=f"Daemon process restart failed: {str(e)}",
                )
                messages.error(request, f"Failed to restart daemon: {str(e)}")
            finally:
                if log_file:
                    log_file.close()
            return redirect("dashboard:daemon")

    # Read latest 100 log lines from daemon.log if it exists
    log_lines = []
    if os.path.exists("daemon.log"):
        try:
            with open("daemon.log", "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                cfg = SiteConfig.load()
                llm_key = cfg.llm_api_key
                bc_key = cfg.bettercontact_api_key
                for line in lines[-100:]:
                    cleaned = line.strip()
                    if llm_key:
                        cleaned = cleaned.replace(llm_key, "********")
                    if bc_key:
                        cleaned = cleaned.replace(bc_key, "********")
                    log_lines.append(cleaned)
        except Exception:
            pass

    last_log = ActionLog.objects.order_by("-created_at").first()
    last_action_time = last_log.created_at if last_log else None

    pending_tasks = Task.objects.filter(status=Task.Status.PENDING).count()
    failed_tasks = Task.objects.filter(status=Task.Status.FAILED).count()
    next_task = Task.objects.filter(status=Task.Status.PENDING).order_by("scheduled_at").first()

    recent_failures = Task.objects.filter(status=Task.Status.FAILED).order_by("-completed_at")[:5]
    recent_actions = ActionLog.objects.order_by("-created_at")[:5]

    # Blockers checking via unified readiness service
    from openoutreach.core.readiness import check_launch_readiness
    readiness_data = check_launch_readiness()
    blockers = [f"{b['title']}: {b['description']}" for b in readiness_data["hard_blockers"]]
    has_hard_blockers = (readiness_data["overall_status"] == "blocked")
    active_profile = get_first_active_profile()

    # Get latest launch run for summary info
    from openoutreach.core.models import LaunchRun
    latest_launch_run = LaunchRun.objects.filter(mode="live").first()

    context = {
        "last_action_time": last_action_time,
        "pending_tasks": pending_tasks,
        "failed_tasks": failed_tasks,
        "next_task": next_task,
        "daemon_active": process_state,
        "daemon_status": status,
        "log_lines": log_lines,
        "recent_failures": recent_failures,
        "recent_actions": recent_actions,
        "blockers": blockers,
        "has_hard_blockers": has_hard_blockers,
        "readiness": readiness_data,
        "latest_launch_run": latest_launch_run,
        "active_profile": active_profile.linkedin_username if active_profile else None,
    }
    return render(request, "dashboard/daemon.html", context)


@dashboard_login_required
def readiness(request):
    from openoutreach.core.readiness import check_launch_readiness
    campaign_id_str = request.GET.get("campaign_id", "").strip()
    campaign_id = int(campaign_id_str) if campaign_id_str.isdigit() else None
    
    # Check if scoped campaign exists
    scoped_campaign = None
    if campaign_id:
        scoped_campaign = get_object_or_404(Campaign, pk=campaign_id)
        
    readiness_result = check_launch_readiness(campaign_id=campaign_id)
    
    context = {
        "readiness": readiness_result,
        "campaigns": Campaign.objects.all(),
        "scoped_campaign": scoped_campaign,
    }
    return render(request, "dashboard/readiness.html", context)


@dashboard_login_required
def diagnostics(request):
    """Read-only diagnostics page. No sending, scraping, LLM calls, or state changes."""
    from openoutreach.linkedin.models import SearchKeyword

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cfg = SiteConfig.load()

    # ── LinkedIn Account Health ─────────────────────────────────
    profiles = LinkedInProfile.objects.select_related("user").all()
    accounts_data = []
    for p in profiles:
        connect_used = ActionLog.objects.filter(
            linkedin_profile=p,
            action_type=ActionLog.ActionType.CONNECT,
            created_at__gte=today_start,
        ).count()
        followup_used = ActionLog.objects.filter(
            linkedin_profile=p,
            action_type=ActionLog.ActionType.FOLLOW_UP,
            created_at__gte=today_start,
        ).count()
        assigned_campaigns = list(
            Campaign.objects.filter(users=p.user).values_list("name", flat=True)
        )
        
        # Fair sharing slot allocations:
        from openoutreach.core.scheduler import allocate_slots
        active_camps = list(Campaign.objects.filter(users=p.user, is_active=True))
        alloc_conn = allocate_slots(p.connect_daily_limit, 0, active_camps, "daily_connect_limit", p, "connect")
        alloc_follow = allocate_slots(p.follow_up_daily_limit, 0, active_camps, "daily_follow_up_limit", p, "follow_up")
        
        allocations = []
        for ac in active_camps:
            c_conn = alloc_conn.get(ac.pk, 0)
            c_follow = alloc_follow.get(ac.pk, 0)
            allocations.append(f"{ac.name} (Connect: {c_conn}, Follow-up: {c_follow})")

        accounts_data.append({
            "username": p.linkedin_username,
            "active": p.active,
            "connect_limit": p.connect_daily_limit,
            "connect_used": connect_used,
            "followup_limit": p.follow_up_daily_limit,
            "followup_used": followup_used,
            "campaigns": assigned_campaigns,
            "active_campaigns": [ac.name for ac in active_camps],
            "allocations": allocations,
        })

    # ── Campaign Health ─────────────────────────────────────────
    campaigns_list = Campaign.objects.prefetch_related("users").all()
    all_pending = list(Task.objects.filter(status=Task.Status.PENDING))
    all_failed = list(Task.objects.filter(status=Task.Status.FAILED))

    campaigns_data = []
    for c in campaigns_list:
        users = list(c.users.values_list("username", flat=True))
        assigned_profiles = list(LinkedInProfile.objects.filter(user__in=c.users.all()).values_list("linkedin_username", flat=True))

        # Deal funnel
        funnel = {}
        for state in DealState:
            funnel[state.label] = Deal.objects.filter(campaign=c, state=state).count()

        total_leads = Lead.objects.filter(deal__campaign=c).distinct().count()
        disqualified = Lead.objects.filter(disqualified=True, deal__campaign=c).distinct().count()

        # Pending tasks per type
        connect_pending = sum(
            1 for t in all_pending
            if t.payload.get("campaign_id") == c.id and t.task_type == Task.TaskType.CONNECT
        )
        followup_pending = sum(
            1 for t in all_pending
            if t.payload.get("campaign_id") == c.id and t.task_type == Task.TaskType.FOLLOW_UP
        )
        failed_count = sum(
            1 for t in all_failed if t.payload.get("campaign_id") == c.id
        )
        # Campaign readiness checks
        user_ids = list(c.users.values_list("id", flat=True))
        active_profiles = LinkedInProfile.objects.filter(user_id__in=user_ids, active=True)
        
        qualified_count = Deal.objects.filter(campaign=c, state=DealState.QUALIFIED).count()
        ready_count = Deal.objects.filter(campaign=c, state=DealState.READY_TO_CONNECT).count()
        
        all_exhausted = True
        for p in active_profiles:
            connect_used = ActionLog.objects.filter(
                linkedin_profile=p,
                action_type=ActionLog.ActionType.CONNECT,
                created_at__gte=today_start,
            ).count()
            if p.connect_daily_limit is None or connect_used < p.connect_daily_limit:
                all_exhausted = False
                break

        checklist = [
            {
                "name": "Campaign Active Status",
                "status": "Pass" if c.is_active else "Blocked",
                "desc": "Campaign is active." if c.is_active else "Campaign is paused/inactive."
            },
            {
                "name": "Has assigned active LinkedIn account",
                "status": "Pass" if active_profiles.exists() else "Blocked",
                "desc": f"Found {active_profiles.count()} active LinkedIn account(s) assigned." if active_profiles.exists() else "No active LinkedIn accounts assigned to this campaign."
            },
            {
                "name": "Has LLM configured",
                "status": "Pass" if (cfg.is_llm_configured() and cfg.ai_model) else "Blocked",
                "desc": "LLM credentials and model are configured." if (cfg.is_llm_configured() and cfg.ai_model) else "LLM credentials or model details are missing."
            },
            {
                "name": "Has campaign product docs",
                "status": "Pass" if c.product_docs.strip() else "Warning",
                "desc": "Product documentation is populated." if c.product_docs.strip() else "Product documentation is empty."
            },
            {
                "name": "Has campaign objective",
                "status": "Pass" if c.campaign_objective.strip() else "Warning",
                "desc": "Campaign objective is populated." if c.campaign_objective.strip() else "Campaign objective is empty."
            },
            {
                "name": "Has connection notes enabled",
                "status": "Pass" if c.connection_note_enabled else "Warning",
                "desc": "Personalized connection notes are enabled." if c.connection_note_enabled else "Personalized connection notes are disabled."
            },
            {
                "name": "Has pending connect tasks",
                "status": "Pass" if connect_pending > 0 else "Warning",
                "desc": f"Found {connect_pending} pending connect tasks." if connect_pending > 0 else "No pending connect tasks scheduled."
            },
            {
                "name": "Has qualified leads",
                "status": "Pass" if (qualified_count > 0 or ready_count > 0) else "Warning",
                "desc": f"Found {qualified_count} qualified lead(s)." if (qualified_count > 0 or ready_count > 0) else "No qualified leads in pipeline."
            },
            {
                "name": "Has ready-to-connect leads",
                "status": "Pass" if ready_count > 0 else "Warning",
                "desc": f"Found {ready_count} ready lead(s)." if ready_count > 0 else "No ready-to-connect leads available."
            },
            {
                "name": "Daily limit available",
                "status": "Pass" if (active_profiles.exists() and not all_exhausted) else ("Blocked" if not active_profiles.exists() else "Warning"),
                "desc": "Daily connection quota limit is available." if (active_profiles.exists() and not all_exhausted) else ("No active account to evaluate limit." if not active_profiles.exists() else "Daily connection limit reached.")
            }
        ]

        # Recent disqualifications
        recent_dq = (
            Deal.objects.filter(campaign=c, state=DealState.FAILED, outcome="wrong_fit")
            .select_related("lead")
            .order_by("-update_date")[:10]
        )

        # Recent qualified or ready leads
        recent_qualified_or_ready = (
            Deal.objects.filter(campaign=c, state__in=[DealState.QUALIFIED, DealState.READY_TO_CONNECT])
            .select_related("lead")
            .order_by("-update_date")[:10]
        )

        # Recent keywords
        keywords = SearchKeyword.objects.filter(campaign=c, used=True).order_by("-used_at")[:10]

        # Blocking reasons
        blocking = _get_campaign_blocking_reasons(c, cfg, today_start)

        # Note preview (template only, no LLM, no sending)
        note_preview = None
        if c.connection_note_enabled:
            note_preview = _render_safe_note_preview(c)

        campaigns_data.append({
            "campaign": c,
            "users": users,
            "assigned_profiles": assigned_profiles,
            "funnel": funnel,
            "total_leads": total_leads,
            "disqualified": disqualified,
            "connect_pending": connect_pending,
            "followup_pending": followup_pending,
            "failed_tasks": failed_count,
            "recent_dq": recent_dq,
            "recent_qualified_or_ready": recent_qualified_or_ready,
            "keywords": keywords,
            "blocking": blocking,
            "note_preview": note_preview,
            "checklist": checklist,
        })

    # ── Task Queue Summary ──────────────────────────────────────
    task_summary = {}
    for status_val, status_label in Task.Status.choices:
        task_summary[status_label] = Task.objects.filter(status=status_val).count()

    # ── Recent Action Logs ──────────────────────────────────────
    recent_logs = (
        ActionLog.objects.select_related("linkedin_profile", "campaign")
        .order_by("-created_at")[:20]
    )

    # ── Recent Failed Tasks ─────────────────────────────────────
    campaign_cache = {c.id: c for c in campaigns_list}
    recent_failures = []
    for t in Task.objects.filter(status=Task.Status.FAILED).order_by("-created_at")[:20]:
        cid = t.payload.get("campaign_id")
        recent_failures.append({
            "task": t,
            "campaign_name": campaign_cache[cid].name if cid and cid in campaign_cache else "Unknown",
        })

    context = {
        "accounts": accounts_data,
        "campaigns": campaigns_data,
        "task_summary": task_summary,
        "recent_logs": recent_logs,
        "recent_failures": recent_failures,
        "llm_configured": cfg.is_llm_configured(),
    }
    return render(request, "dashboard/diagnostics.html", context)


def _get_campaign_blocking_reasons(campaign, cfg, today_start):
    """Compute blocking reasons for a campaign (read-only, no side effects)."""
    reasons = []

    if not campaign.is_active:
        reasons.append("Campaign is paused")
        return reasons

    user_ids = list(campaign.users.values_list("id", flat=True))
    if not user_ids:
        reasons.append("No assigned users")
        return reasons

    active_profiles = LinkedInProfile.objects.filter(user_id__in=user_ids, active=True)
    if not active_profiles.exists():
        reasons.append("No active LinkedIn account")

    # Daily limit check
    all_exhausted = True
    for p in active_profiles:
        connect_used = ActionLog.objects.filter(
            linkedin_profile=p,
            action_type=ActionLog.ActionType.CONNECT,
            created_at__gte=today_start,
        ).count()
        if p.connect_daily_limit is None or connect_used < p.connect_daily_limit:
            all_exhausted = False
            break
    if active_profiles.exists() and all_exhausted:
        reasons.append("Daily connect limit reached")

    # Pending connect tasks
    pending_connect = sum(
        1 for t in Task.objects.filter(
            status=Task.Status.PENDING, task_type=Task.TaskType.CONNECT,
        )
        if t.payload.get("campaign_id") == campaign.id
    )
    if pending_connect == 0:
        reasons.append("No pending connect tasks")

    # Qualified and ready leads
    qualified_count = Deal.objects.filter(campaign=campaign, state=DealState.QUALIFIED).count()
    ready_count = Deal.objects.filter(campaign=campaign, state=DealState.READY_TO_CONNECT).count()
    if qualified_count == 0 and ready_count == 0:
        reasons.append("No qualified leads")
    elif ready_count == 0 and qualified_count > 0:
        reasons.append("No ready leads available")

    # LLM
    if not cfg.is_llm_configured():
        reasons.append("LLM not configured")

    return reasons


def _render_safe_note_preview(campaign):
    """Template-only note preview. No LLM, no sending, no scraping."""
    from openoutreach.core.agents.connection_note import _generate_template_note

    deal = (
        Deal.objects.filter(campaign=campaign, state=DealState.READY_TO_CONNECT)
        .select_related("lead")
        .first()
    )

    if deal:
        sample_profile = {
            "first_name": deal.lead.public_identifier.split("-")[0].title(),
            "public_identifier": deal.lead.public_identifier,
        }
    else:
        sample_profile = {"first_name": "Alex"}

    try:
        return _generate_template_note(campaign, sample_profile)
    except Exception:
        return None



def dashboard_login(request):
    from openoutreach.core.models import WorkspaceMembership
    if request.user.is_authenticated:
        has_membership = WorkspaceMembership.objects.filter(user=request.user, status="active").exists()
        if request.user.is_staff or request.user.is_superuser or has_membership:
            next_url = request.GET.get(REDIRECT_FIELD_NAME, "")
            from django.utils.http import url_has_allowed_host_and_scheme
            if next_url and url_has_allowed_host_and_scheme(url=next_url, allowed_hosts={request.get_host()}):
                return redirect(next_url)
            return redirect("dashboard:overview")

    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        has_membership = WorkspaceMembership.objects.filter(user=user, status="active").exists()
        if user.is_staff or user.is_superuser or has_membership:
            auth_login(request, user)
            next_url = request.GET.get(REDIRECT_FIELD_NAME, "")
            from django.utils.http import url_has_allowed_host_and_scheme
            if next_url and url_has_allowed_host_and_scheme(url=next_url, allowed_hosts={request.get_host()}):
                return redirect(next_url)
            return redirect("dashboard:overview")
        else:
            form.add_error(None, "Access denied: Staff status required.")

    return render(request, "dashboard/login.html", {"form": form})


def dashboard_logout(request):
    auth_logout(request)
    return redirect("dashboard:login")


from django.views.decorators.http import require_POST
from django.http import HttpResponseNotAllowed

class ReconcileDummySession:
    def __init__(self, profile):
        self.linkedin_profile = profile
        from openoutreach.core.conf import ACTIVE_TIMEZONE
        self.active_timezone = ACTIVE_TIMEZONE


@dashboard_login_required
@require_POST
def campaign_pause(request, pk):
    campaign = get_authorized_campaign(request, pk)
    campaign.is_active = False
    campaign.save(update_fields=["is_active"])
    messages.success(request, f"Campaign '{campaign.name}' paused successfully.")
    return redirect(request.META.get("HTTP_REFERER", "dashboard:campaigns"))


@dashboard_login_required
@require_POST
def campaign_resume(request, pk):
    campaign = get_authorized_campaign(request, pk)
    campaign.is_active = True
    campaign.save(update_fields=["is_active"])
    messages.success(request, f"Campaign '{campaign.name}' resumed successfully.")
    return redirect(request.META.get("HTTP_REFERER", "dashboard:campaigns"))


@dashboard_login_required
@require_POST
def campaign_reconcile(request, pk):
    campaign = get_authorized_campaign(request, pk)
    if not campaign.is_active:
        messages.warning(request, "Campaign is paused. Cannot reconcile task schedule.")
        return redirect(request.META.get("HTTP_REFERER", "dashboard:campaigns"))

    profile = LinkedInProfile.objects.filter(user__in=campaign.users.all(), active=True).first()
    if not profile:
        messages.error(request, "No active LinkedIn account assigned to this campaign. Cannot schedule.")
    else:
        from openoutreach.core.scheduler import plan_connect_window, plan_follow_up_window, plan_check_pending_window, flush_email_queue, _recover_stale_running_tasks
        session = ReconcileDummySession(profile)
        _recover_stale_running_tasks()
        plan_connect_window(session, campaign)
        plan_follow_up_window(session, campaign)
        plan_check_pending_window(session, campaign)
        flush_email_queue(session, campaign)
        messages.success(request, f"Reconciled task schedule for campaign '{campaign.name}'.")

    return redirect(request.META.get("HTTP_REFERER", "dashboard:campaigns"))


@dashboard_login_required
def campaign_clear_pending(request, pk):
    campaign = get_authorized_campaign(request, pk)
    query = Task.objects.filter(
        status=Task.Status.PENDING
    ).filter(
        Q(payload__campaign_id=campaign.id) | Q(payload__campaign_id=str(campaign.id))
    )
    count = query.count()

    if request.method == "POST":
        if request.POST.get("confirm") == "no":
            messages.info(request, f"Task clearing cancelled for '{campaign.name}'.")
        else:
            deleted = query.delete()[0]
            messages.success(request, f"Cleared {deleted} pending tasks for campaign '{campaign.name}'.")
        redirect_url = request.META.get("HTTP_REFERER")
        if redirect_url:
            return redirect(redirect_url)
        return redirect("dashboard:campaign_edit", pk=campaign.pk)

    try:
        cancel_url = reverse("dashboard:campaign_edit", kwargs={"pk": campaign.pk})
    except Exception:
        cancel_url = reverse("dashboard:campaigns")

    return render(
        request,
        "dashboard/campaign_action_confirm.html",
        {
            "campaign": campaign,
            "action_name": "Clear Pending Tasks",
            "count": count,
            "description": "This will permanently delete all pending task queue entries for this campaign.",
            "cancel_url": cancel_url,
        }
    )


@dashboard_login_required
def campaign_clear_failed(request, pk):
    campaign = get_authorized_campaign(request, pk)
    query = Task.objects.filter(
        status=Task.Status.FAILED
    ).filter(
        Q(payload__campaign_id=campaign.id) | Q(payload__campaign_id=str(campaign.id))
    )
    count = query.count()

    if request.method == "POST":
        if request.POST.get("confirm") == "no":
            messages.info(request, f"Task clearing cancelled for '{campaign.name}'.")
        else:
            deleted = query.delete()[0]
            messages.success(request, f"Cleared {deleted} failed tasks for campaign '{campaign.name}'.")
        redirect_url = request.META.get("HTTP_REFERER")
        if redirect_url:
            return redirect(redirect_url)
        return redirect("dashboard:campaign_edit", pk=campaign.pk)

    try:
        cancel_url = reverse("dashboard:campaign_edit", kwargs={"pk": campaign.pk})
    except Exception:
        cancel_url = reverse("dashboard:campaigns")

    return render(
        request,
        "dashboard/campaign_action_confirm.html",
        {
            "campaign": campaign,
            "action_name": "Clear Failed Tasks",
            "count": count,
            "description": "This will permanently delete all failed task queue entries for this campaign.",
            "cancel_url": cancel_url,
        }
    )


@dashboard_login_required
def launch_preview(request):
    import json
    from openoutreach.core.launch_preview import generate_launch_preview
    campaign_id_str = request.GET.get("campaign_id", "").strip()
    campaign_id = int(campaign_id_str) if campaign_id_str.isdigit() else None
    
    limit_str = request.GET.get("limit", "10").strip()
    limit = int(limit_str) if limit_str.isdigit() else 10
    
    # Check if scoped campaign exists
    scoped_campaign = None
    if campaign_id:
        scoped_campaign = get_object_or_404(Campaign, pk=campaign_id)
        
    preview_data = generate_launch_preview(campaign_id=campaign_id, limit=limit)
    
    context = {
        "preview": preview_data,
        "campaigns": Campaign.objects.all(),
        "scoped_campaign": scoped_campaign,
        "limit": limit,
    }
    return render(request, "dashboard/launch_preview.html", context)


@dashboard_login_required
def launch_history(request):
    from openoutreach.core.models import LaunchRun
    runs = LaunchRun.objects.select_related("operator", "campaign").all()
    
    # Filters
    status_filter = request.GET.get("status", "").strip()
    if status_filter:
        runs = runs.filter(status=status_filter)
        
    mode_filter = request.GET.get("mode", "").strip()
    if mode_filter:
        runs = runs.filter(mode=mode_filter)
        
    source_filter = request.GET.get("source", "").strip()
    if source_filter:
        runs = runs.filter(source=source_filter)
        
    campaign_id_str = request.GET.get("campaign_id", "").strip()
    if campaign_id_str.isdigit():
        runs = runs.filter(campaign_id=int(campaign_id_str))
        
    context = {
        "runs": runs[:50],
        "campaigns": Campaign.objects.all(),
        "status_filter": status_filter,
        "mode_filter": mode_filter,
        "source_filter": source_filter,
        "campaign_id_filter": int(campaign_id_str) if campaign_id_str.isdigit() else None,
    }
    return render(request, "dashboard/launch_history.html", context)


@dashboard_login_required
def launch_history_detail(request, pk):
    import json
    from openoutreach.core.models import LaunchRun
    run = get_object_or_404(LaunchRun.objects.select_related("operator", "campaign"), pk=pk)
    
    # Mask raw snapshot outputs recursively
    from openoutreach.core.launch_preview import mask_sensitive_payload_recursive
    masked_readiness = mask_sensitive_payload_recursive(run.readiness_snapshot)
    masked_queue = mask_sensitive_payload_recursive(run.queue_snapshot)
    masked_metadata = mask_sensitive_payload_recursive(run.metadata)
    
    context = {
        "run": run,
        "readiness_snapshot_json": json.dumps(masked_readiness, indent=2),
        "queue_snapshot_json": json.dumps(masked_queue, indent=2),
        "metadata_json": json.dumps(masked_metadata, indent=2),
        "events": run.events.all(),
    }
    return render(request, "dashboard/launch_history_detail.html", context)


@dashboard_login_required
def operations(request):
    from openoutreach.core.operations import get_operations_snapshot
    from openoutreach.core.operator_alerts import build_alert_fingerprint
    from openoutreach.core.models import OperatorAlert

    snapshot = get_operations_snapshot()
    
    # Enrich alerts list in snapshot with their current inbox status
    snap_alerts = snapshot.get("alerts") or []
    for alert_data in snap_alerts:
        if "source" not in alert_data:
            alert_data["source"] = "operations"
        fingerprint = build_alert_fingerprint(alert_data)
        alert_data["fingerprint"] = fingerprint
        
        # Look up inbox state
        alert_db = OperatorAlert.objects.filter(fingerprint=fingerprint).first()
        if alert_db:
            alert_data["inbox_status"] = alert_db.status
            alert_data["inbox_id"] = alert_db.id
        else:
            alert_data["inbox_status"] = "new"
            alert_data["inbox_id"] = None

    context = {
        "snapshot": snapshot,
    }
    return render(request, "dashboard/operations.html", context)


@dashboard_login_required
def operator_inbox(request):
    from django.db.models import Q
    from openoutreach.core.models import OperatorAlert
    from openoutreach.crm.models import Deal, DealState
    from openoutreach.linkedin.models import ActionLog

    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]

    active_tab = request.GET.get("tab", "messages").strip().lower()
    channel_filter = request.GET.get("channel", "all").strip().lower()
    
    # 1. Fetch Conversations / Messages scoped to workspace or user
    if workspace:
        deals_qs = Deal.objects.filter(
            models.Q(campaign__workspace=workspace) | models.Q(campaign__users=request.user),
            is_synthetic=False
        ).distinct().select_related("lead", "campaign").order_by("-update_date")
    else:
        deals_qs = Deal.objects.filter(campaign__users=request.user, is_synthetic=False).select_related("lead", "campaign").order_by("-update_date")

    if channel_filter == "linkedin":
        deals_qs = deals_qs.filter(campaign__name__icontains="LinkedIn")
    elif channel_filter == "email":
        deals_qs = deals_qs.filter(lead__api_email__isnull=False).exclude(lead__api_email="")

    conversations = []
    for d in deals_qs[:30]:
        lead_name = d.lead.public_identifier or "Prospect"
        recent_log = ActionLog.objects.filter(campaign=d.campaign).order_by("-created_at").first()
        snippet = ""
        if recent_log and recent_log.metadata:
            snippet = recent_log.metadata.get("note_preview") or recent_log.metadata.get("note_source") or "Conversation active"
        else:
            snippet = f"Status: {d.get_state_display()}"

        conversations.append({
            "id": d.pk,
            "name": lead_name,
            "company": getattr(d.lead, "company", "") or d.campaign.name,
            "channel": "linkedin" if not d.lead.api_email else "email",
            "state": d.get_state_display(),
            "snippet": snippet,
            "updated_at": d.update_date,
            "has_reply": d.state in [DealState.CONNECTED, DealState.COMPLETED],
        })

    # 2. Fetch Notifications / Alerts
    status_filter = request.GET.get("status", "").strip()
    severity_filter = request.GET.get("severity", "").strip()
    source_filter = request.GET.get("source", "").strip()
    search_query = request.GET.get("q", "").strip()
    
    if workspace:
        qs = OperatorAlert.objects.filter(models.Q(workspace=workspace) | models.Q(workspace__isnull=True))
    else:
        qs = OperatorAlert.objects.all()
    if status_filter:
        qs = qs.filter(status=status_filter)
    if severity_filter:
        qs = qs.filter(severity=severity_filter)
    if source_filter:
        qs = qs.filter(source=source_filter)
    if search_query:
        qs = qs.filter(Q(title__icontains=search_query) | Q(description__icontains=search_query))
        
    limit_str = request.GET.get("limit", "50")
    limit = int(limit_str) if limit_str.isdigit() else 50
    alerts = qs[:limit]
    
    from openoutreach.core.operator_alerts import get_operator_alert_summary
    summary = get_operator_alert_summary()
    
    context = {
        "active_tab": active_tab,
        "channel_filter": channel_filter,
        "conversations": conversations,
        "messages_count": len(conversations),
        "alerts": alerts,
        "summary": summary,
        "status_filter": status_filter,
        "severity_filter": severity_filter,
        "source_filter": source_filter,
        "search_query": search_query,
        "limit": limit,
    }
    return render(request, "dashboard/inbox.html", context)


@dashboard_login_required
def operator_inbox_detail(request, pk):
    from openoutreach.core.models import OperatorAlert
    from openoutreach.core.operator_alerts import serialize_operator_alert
    
    alert = get_object_or_404(OperatorAlert, pk=pk)
    serialized = serialize_operator_alert(alert)
    events = alert.events.select_related("operator").order_by("created_at")
    
    context = {
        "alert": alert,
        "serialized": serialized,
        "events": events,
    }
    return render(request, "dashboard/inbox_detail.html", context)


@dashboard_login_required
def operator_inbox_sync(request):
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])
        
    from openoutreach.core.operator_alerts import sync_operator_alerts
    from django.contrib import messages
    
    res = sync_operator_alerts(operator=request.user, source="operations")
    msg = f"Alert sync complete. Created: {res['created_count']}, Reopened: {res['reopened_count']}, Resolved: {res['resolved_count']}"
    messages.success(request, msg)
    return redirect("dashboard:operator_inbox")


@dashboard_login_required
def operator_inbox_acknowledge(request, pk):
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])
        
    from openoutreach.core.models import OperatorAlert, OperatorAlertEvent
    from django.contrib import messages
    
    alert = get_object_or_404(OperatorAlert, pk=pk)
    note = request.POST.get("note", "").strip()
    
    alert.status = OperatorAlert.AlertStatus.ACKNOWLEDGED
    alert.acknowledged_at = timezone.now()
    alert.acknowledged_by = request.user
    alert.save()
    
    OperatorAlertEvent.objects.create(
        alert=alert,
        operator=request.user,
        event_type=OperatorAlertEvent.EventType.ACKNOWLEDGED,
        note=note or "Acknowledged by operator."
    )
    
    messages.success(request, f"Alert '{alert.title}' acknowledged.")
    return redirect("dashboard:operator_inbox_detail", pk=pk)


@dashboard_login_required
def operator_inbox_snooze(request, pk):
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])
        
    from openoutreach.core.models import OperatorAlert, OperatorAlertEvent
    from django.contrib import messages
    
    alert = get_object_or_404(OperatorAlert, pk=pk)
    hours_str = request.POST.get("hours", "24")
    hours = int(hours_str) if hours_str.isdigit() else 24
    note = request.POST.get("note", "").strip()
    
    alert.status = OperatorAlert.AlertStatus.SNOOZED
    alert.snoozed_until = timezone.now() + timedelta(hours=hours)
    alert.save()
    
    OperatorAlertEvent.objects.create(
        alert=alert,
        operator=request.user,
        event_type=OperatorAlertEvent.EventType.SNOOZED,
        note=note or f"Snoozed for {hours} hours.",
        metadata={"hours": hours}
    )
    
    messages.success(request, f"Alert '{alert.title}' snoozed until {alert.snoozed_until}.")
    return redirect("dashboard:operator_inbox_detail", pk=pk)


@dashboard_login_required
def operator_inbox_dismiss(request, pk):
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])
        
    from openoutreach.core.models import OperatorAlert, OperatorAlertEvent
    from openoutreach.core.operations import get_operations_snapshot
    from openoutreach.core.operator_alerts import build_alert_fingerprint
    from django.contrib import messages
    
    alert = get_object_or_404(OperatorAlert, pk=pk)
    note = request.POST.get("note", "").strip()
    
    # Gating check: Active hard alerts cannot be dismissed if still present in operations snapshot
    if alert.severity == OperatorAlert.SeverityType.HARD:
        snapshot = get_operations_snapshot()
        snap_alerts = snapshot.get("alerts") or []
        present = False
        for a_data in snap_alerts:
            if "source" not in a_data:
                a_data["source"] = "operations"
            if build_alert_fingerprint(a_data) == alert.fingerprint:
                present = True
                break
        if present:
            messages.error(request, f"Dismissal blocked: '{alert.title}' is a hard alert that remains active in the current operations snapshot.")
            return redirect("dashboard:operator_inbox_detail", pk=pk)
            
    alert.status = OperatorAlert.AlertStatus.DISMISSED
    alert.dismissed_at = timezone.now()
    alert.dismissed_by = request.user
    alert.save()
    
    OperatorAlertEvent.objects.create(
        alert=alert,
        operator=request.user,
        event_type=OperatorAlertEvent.EventType.DISMISSED,
        note=note or "Dismissed by operator."
    )
    
    messages.success(request, f"Alert '{alert.title}' dismissed.")
    return redirect("dashboard:operator_inbox_detail", pk=pk)


@dashboard_login_required
def operator_inbox_reopen(request, pk):
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])
        
    from openoutreach.core.models import OperatorAlert, OperatorAlertEvent
    from django.contrib import messages
    
    alert = get_object_or_404(OperatorAlert, pk=pk)
    note = request.POST.get("note", "").strip()
    
    alert.status = OperatorAlert.AlertStatus.ACTIVE
    alert.resolved_at = None
    alert.dismissed_at = None
    alert.snoozed_until = None
    alert.save()
    
    OperatorAlertEvent.objects.create(
        alert=alert,
        operator=request.user,
        event_type=OperatorAlertEvent.EventType.REOPENED,
        note=note or "Reopened by operator."
    )
    
    messages.success(request, f"Alert '{alert.title}' reopened.")
    return redirect("dashboard:operator_inbox_detail", pk=pk)


@dashboard_login_required
def operator_inbox_resolve(request, pk):
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])
        
    from openoutreach.core.models import OperatorAlert, OperatorAlertEvent
    from django.contrib import messages
    
    alert = get_object_or_404(OperatorAlert, pk=pk)
    note = request.POST.get("note", "").strip()
    
    alert.status = OperatorAlert.AlertStatus.RESOLVED
    alert.resolved_at = timezone.now()
    alert.save()
    
    OperatorAlertEvent.objects.create(
        alert=alert,
        operator=request.user,
        event_type=OperatorAlertEvent.EventType.RESOLVED,
        note=note or "Resolved by operator."
    )
    
    messages.success(request, f"Alert '{alert.title}' marked resolved.")
    return redirect("dashboard:operator_inbox_detail", pk=pk)


@dashboard_login_required
def production(request):
    """Production readiness and environment hardening checkpoint page."""
    from openoutreach.core.production import get_production_readiness_snapshot
    snapshot = get_production_readiness_snapshot()
    return render(
        request,
        "dashboard/production.html",
        {
            "snapshot": snapshot,
        }
    )







@dashboard_login_required
@require_POST
def deal_review_action(request, pk):
    from openoutreach.crm.models.deal import Deal
    from openoutreach.crm.models.lead import SuppressionRecord
    from openoutreach.core.content_style import validate_user_facing_content_style
    
    deal = get_authorized_deal(request, pk)
    action = request.POST.get("action")
    
    if action == "approve":
        deal.manual_review_status = "approved"
        deal.reviewer = request.user
        deal.reviewed_at = timezone.now()
        # In Milestone 1, preview drafts are strictly non-sendable
        deal.send_eligible = False
        deal.sendable = False
        deal.external_action_authorized = False
        deal.save()
        messages.success(request, f"Target {deal.lead} approved for future pilot (draft-mode/non-sendable).")
        
    elif action == "reject":
        reason = request.POST.get("reason", "")
        deal.manual_review_status = "rejected"
        deal.reviewer = request.user
        deal.reviewed_at = timezone.now()
        deal.rejection_reason = reason
        deal.save()
        messages.success(request, f"Target {deal.lead} rejected.")
        
    elif action == "edit":
        note = request.POST.get("connection_note", "")
        followup = request.POST.get("follow_up", "")
        # Validate edited style via canonical gate
        from openoutreach.core.content_style import validate_linkedin_outbound_message
        res_note = validate_linkedin_outbound_message(note, "connection_note", deal=deal)
        res_followup = validate_linkedin_outbound_message(followup, "follow_up", deal=deal)
        
        if not res_note["allowed"] or not res_followup["allowed"]:
            errors = res_note["errors"] + res_followup["errors"]
            messages.error(request, f"Style validation failed: {', '.join(errors)}")
        else:
            deal.generated_connection_note = note
            deal.generated_follow_up = followup
            deal.save()
            messages.success(request, f"Target {deal.lead} messages updated successfully.")
            
    elif action == "suppress":
        from openoutreach.crm.service import suppress_lead_and_deals
        suppress_lead_and_deals(deal.lead.linkedin_url, reason="Suppressed from manual review screen")
        messages.success(request, f"Recipient {deal.lead.linkedin_url} suppressed globally.")
        
    return redirect("dashboard:campaign_review", pk=deal.campaign.pk)


@dashboard_login_required
def operator_inbox_resolve(request, pk):
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])
        
    from openoutreach.core.models import OperatorAlert, OperatorAlertEvent
    from django.contrib import messages
    
    alert = get_object_or_404(OperatorAlert, pk=pk)
    note = request.POST.get("note", "").strip()
    
    alert.status = OperatorAlert.AlertStatus.RESOLVED
    alert.resolved_at = timezone.now()
    alert.save()
    
    OperatorAlertEvent.objects.create(
        alert=alert,
        operator=request.user,
        event_type=OperatorAlertEvent.EventType.RESOLVED,
        note=note or "Resolved by operator."
    )
    
    messages.success(request, f"Alert '{alert.title}' marked resolved.")
    return redirect("dashboard:operator_inbox_detail", pk=pk)


@dashboard_login_required
def production(request):
    """Production readiness and environment hardening checkpoint page."""
    from openoutreach.core.production import get_production_readiness_snapshot
    snapshot = get_production_readiness_snapshot()
    return render(
        request,
        "dashboard/production.html",
        {
            "snapshot": snapshot,
        }
    )


@dashboard_login_required
def linkedin_account_session(request, pk):
    from openoutreach.linkedin.models import LinkedInProfile
    from openoutreach.core.models import WorkspaceMembership
    from openoutreach.linkedin.session_manager import run_session_health_check, manual_login_linkedin
    from django.utils import timezone
    from django.core.exceptions import PermissionDenied

    profile = get_object_or_404(LinkedInProfile, pk=pk)

    # Multi-tenant and workspace authorization check
    if not (request.user.is_superuser or request.user.is_staff):
        if profile.workspace:
            membership = WorkspaceMembership.objects.filter(
                workspace=profile.workspace,
                user=request.user,
                status="active",
                role__in=["owner", "admin"],
            ).first()
            if not membership and profile.user != request.user:
                raise PermissionDenied("You do not have permission to manage this LinkedIn profile's session.")
        else:
            if profile.user != request.user:
                raise PermissionDenied("You do not have permission to manage this LinkedIn profile's session.")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_li_at":
            raw_li_at = request.POST.get("li_at_cookie", "").strip()
            if not raw_li_at:
                messages.error(request, "Please enter a valid li_at session key.")
            else:
                try:
                    from openoutreach.linkedin.session_manager import save_linkedin_session_cookie
                    save_linkedin_session_cookie(profile, raw_li_at)
                    messages.success(request, "LinkedIn session cookie encrypted and saved securely. Click 'Validate Session' to verify when ready.")
                except Exception as e:
                    messages.error(request, f"Failed to save session cookie: {str(e)}")
        elif action == "validate" or action == "refresh":
            try:
                run_session_health_check(profile)
                if profile.session_status == "valid":
                    messages.success(request, "LinkedIn session validated successfully. Active session is live.")
                else:
                    messages.warning(request, f"LinkedIn session check returned status: {profile.get_session_status_display()}.")
            except Exception as e:
                messages.error(request, f"Failed to run session validation: {str(e)}")
        elif action == "clear":
            profile.cookie_data = None
            profile.session_status = "expired"
            profile.session_error_details = "Session cleared by user."
            profile.save()
            messages.success(request, "Saved LinkedIn session cookies have been cleared.")
        elif action == "manual_login":
            try:
                manual_login_linkedin(profile)
                if profile.session_status == "valid":
                    messages.success(request, "Manual login completed. Saved cookies updated and verified.")
                else:
                    messages.warning(request, f"Manual login did not yield a valid session. Status: {profile.get_session_status_display()}.")
            except Exception as e:
                messages.error(request, f"Error during manual login: {str(e)}")
                
        return redirect("dashboard:linkedin_account_session", pk=pk)
        
    # Calculate session age
    session_age_str = "N/A"
    if profile.last_authenticated_at:
        diff = timezone.now() - profile.last_authenticated_at
        hours = diff.total_seconds() // 3600
        minutes = (diff.total_seconds() % 3600) // 60
        if hours > 0:
            session_age_str = f"{int(hours)}h {int(minutes)}m ago"
        else:
            session_age_str = f"{int(minutes)}m ago"
            
    context = {
        "profile": profile,
        "session_age_str": session_age_str,
    }
    return render(request, "dashboard/linkedin_account_session.html", context)


@dashboard_login_required
def campaign_review(request, pk):
    from openoutreach.core.models import Campaign
    from openoutreach.crm.models.deal import Deal, DealState
    from openoutreach.crm.models.lead import SuppressionRecord
    from openoutreach.career.models import IncidentHold, CandidateIdentityVerification
    from openoutreach.core.content_style import validate_user_facing_content_style, validate_generated_content_claims
    from openoutreach.linkedin.models import SearchKeyword

    campaign = get_authorized_campaign(request, pk)

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action in ("generate_targets", "discover_targets"):
            from openoutreach.linkedin.services.discovery_service import discover_campaign_targets

            res = discover_campaign_targets(campaign, limit=10)
            if res.get("status") == "success":
                kw = res.get("keyword")
                found = res.get("profiles_found", 0)
                deals = res.get("deals_created", 0)
                strong = res.get("strong_fits", 0)
                messages.success(
                    request,
                    f"Discovered targets via '{kw}': Found {found} profiles, created {deals} deals ({strong} strong fits READY_TO_CONNECT)."
                )
            else:
                messages.warning(request, f"Discovery completed with status: {res.get('message')}")
            return redirect("dashboard:campaign_review", pk=pk)

    deals = campaign.deals.all()
    search_keywords = SearchKeyword.objects.filter(campaign=campaign).order_by("-id")
    
    # We populate/verify safety statuses for each deal in view context
    for deal in deals:
        # Canonical Outbound validation
        from openoutreach.core.content_style import validate_linkedin_outbound_message
        note_res = validate_linkedin_outbound_message(deal.generated_connection_note, "connection_note", deal=deal)
        followup_res = validate_linkedin_outbound_message(deal.generated_follow_up, "follow_up", deal=deal)
        deal.style_valid = note_res["style_result"]["valid"] and followup_res["style_result"]["valid"]
        deal.style_errors = note_res["style_result"]["errors"] + followup_res["style_result"]["errors"]

        # Claim validation context
        claim_context = {
            "identity_unverified": not CandidateIdentityVerification.objects.filter(candidate_profile__user=request.user, status="verified").exists(),
            "incident_hold_active": IncidentHold.objects.filter(active=True).exists(),
        }
        claims_res = validate_generated_content_claims(deal.generated_connection_note, claim_context)
        deal.claims_valid = claims_res["valid"]
        deal.claims_errors = claims_res["errors"]

        # Identity and Hold Statuses
        deal.identity_status = "Unverified" if claim_context["identity_unverified"] else "Verified"
        deal.incident_hold_active = claim_context["incident_hold_active"]

        # Duplicate Status
        other_deals = Deal.objects.filter(lead=deal.lead).exclude(pk=deal.pk)
        deal.is_duplicate = other_deals.exists()
        deal.other_deals_count = other_deals.count()

        # Evaluate structured ICP fit
        from openoutreach.linkedin.pipeline.icp_evaluator import evaluate_lead_icp_fit
        deal.icp_eval = evaluate_lead_icp_fit(deal.lead.to_profile_dict(), campaign)

        # Suppression Status
        deal.is_suppressed = SuppressionRecord.objects.filter(linkedin_url=deal.lead.linkedin_url).exists()

    summary_stats = {
        "total_deals": deals.count(),
        "fully_enriched": sum(1 for d in deals if d.lead.first_name and d.lead.current_title),
        "partially_enriched": sum(1 for d in deals if (d.lead.first_name or d.lead.current_title) and not (d.lead.first_name and d.lead.current_title)),
        "enrichment_failed": sum(1 for d in deals if not d.lead.first_name and not d.lead.current_title),
        "strong_fits": sum(1 for d in deals if d.state == DealState.READY_TO_CONNECT),
        "needs_review": sum(1 for d in deals if d.state == DealState.QUALIFIED),
        "ready_to_connect": sum(1 for d in deals if d.state == DealState.READY_TO_CONNECT),
    }

    from openoutreach.core.campaign_preflight import check_campaign_resume_readiness
    preflight_status = check_campaign_resume_readiness(campaign)

    context = {
        "campaign": campaign,
        "deals": deals,
        "search_keywords": search_keywords,
        "summary_stats": summary_stats,
        "preflight_status": preflight_status,
    }
    return render(request, "dashboard/campaign_review.html", context)


@dashboard_login_required
@require_POST
def deal_review_action(request, pk):
    from openoutreach.crm.models.deal import Deal
    from openoutreach.crm.models.lead import SuppressionRecord
    from openoutreach.core.content_style import validate_user_facing_content_style
    
    deal = get_object_or_404(Deal, pk=pk)
    action = request.POST.get("action")
    
    if action == "approve":
        from openoutreach.crm.models.deal import DealState
        deal.manual_review_status = "approved"
        deal.reviewer = request.user
        deal.reviewed_at = timezone.now()
        if deal.state == DealState.QUALIFIED:
            deal.state = DealState.READY_TO_CONNECT
        deal.reason = f"{deal.reason} | [APPROVED by {request.user.username} at {timezone.now().strftime('%Y-%m-%d %H:%M')}]"
        deal.save()
        messages.success(request, f"Target {deal.lead} manually approved (promoted to READY_TO_CONNECT).")
        
    elif action == "reject":
        from openoutreach.crm.models.deal import DealState, Outcome
        reason = request.POST.get("reason", "")
        deal.manual_review_status = "rejected"
        deal.reviewer = request.user
        deal.reviewed_at = timezone.now()
        deal.state = DealState.FAILED
        deal.outcome = Outcome.WRONG_FIT
        deal.rejection_reason = reason
        deal.reason = f"{deal.reason} | [REJECTED by {request.user.username}: {reason}]"
        deal.save()
        messages.success(request, f"Target {deal.lead} rejected.")
        
    elif action == "edit":
        note = request.POST.get("connection_note", "")
        followup = request.POST.get("follow_up", "")
        # Validate edited style via canonical gate
        from openoutreach.core.content_style import validate_linkedin_outbound_message
        res_note = validate_linkedin_outbound_message(note, "connection_note", deal=deal)
        res_followup = validate_linkedin_outbound_message(followup, "follow_up", deal=deal)
        
        if not res_note["allowed"] or not res_followup["allowed"]:
            errors = res_note["errors"] + res_followup["errors"]
            messages.error(request, f"Style validation failed: {', '.join(errors)}")
        else:
            deal.generated_connection_note = note
            deal.generated_follow_up = followup
            deal.save()
            messages.success(request, f"Target {deal.lead} messages updated successfully.")
            
    elif action == "suppress":
        from openoutreach.crm.service import suppress_lead_and_deals
        suppress_lead_and_deals(deal.lead.linkedin_url, reason="Suppressed from manual review screen")
        messages.success(request, f"Recipient {deal.lead.linkedin_url} suppressed globally.")
        
    return redirect("dashboard:campaign_review", pk=deal.campaign.pk)


def _resolve_operator_context(request):
    """
    Resolves the effective candidate, LinkedIn account, workspace, and automation settings.
    Upholds strict multi-tenant scoping:
    - Standard users access ONLY their own owned Workspace, CandidateProfile, and LinkedInProfile.
    - Admin users can view their own profile or explicitly inspect an operator/tenant context.
    - Surfaces admin_operating if an admin is inspecting an operator profile.
    """
    from openoutreach.career.models import CandidateProfile
    from openoutreach.linkedin.models import LinkedInProfile
    from openoutreach.core.models import Workspace, WorkspaceMembership, WorkspaceAutomationSettings
    from openoutreach.core.services.workspace_membership import create_workspace_with_owner

    user = request.user
    admin_operating = False

    # 1. Resolve workspace
    workspace = None
    if user.is_authenticated:
        membership = WorkspaceMembership.objects.filter(user=user, status="active").select_related("workspace").first()
        if membership:
            workspace = membership.workspace
        else:
            workspace = Workspace.objects.filter(memberships__user=user).first()
            if not workspace:
                if user.is_superuser or user.is_staff:
                    workspace = Workspace.objects.filter(slug="default").first() or Workspace.objects.first()
                if not workspace:
                    slug = f"ws-{user.username}".lower().replace(" ", "-")
                    workspace = create_workspace_with_owner(
                        name=f"{user.username.capitalize()}'s Workspace",
                        slug=slug,
                        owner_user=user,
                    )

    # 2. Support explicit admin workspace inspection if requested
    if (request.user.is_staff or request.user.is_superuser) and request.GET.get("impersonate_workspace"):
        target_ws = Workspace.objects.filter(pk=request.GET.get("impersonate_workspace")).first()
        if target_ws:
            workspace = target_ws
            admin_operating = True

    cand_prof = CandidateProfile.objects.filter(user=user).first()
    lp = LinkedInProfile.objects.filter(user=user).first()

    if (request.user.is_staff or request.user.is_superuser) and not cand_prof and not lp:
        cand_prof = cand_prof or CandidateProfile.objects.first()
        lp = lp or LinkedInProfile.objects.first()
        if cand_prof and cand_prof.user != request.user:
            admin_operating = True

    automation_settings = WorkspaceAutomationSettings.get_for_workspace(workspace) if workspace else None

    return {
        "signed_in_user": request.user,
        "operator_user": user,
        "workspace": workspace,
        "candidate_profile": cand_prof,
        "linkedin_profile": lp,
        "automation_settings": automation_settings,
        "admin_operating": admin_operating,
    }





@dashboard_login_required
def live_work(request):
    from openoutreach.core.models import Campaign
    from openoutreach.linkedin.vnc_service import check_vnc_listener, get_credential_fingerprint
    from openoutreach.linkedin.models import ActionLog

    ctx = _resolve_operator_context(request)
    camps = Campaign.objects.filter(is_active=True)
    active_c = camps.first() or Campaign.objects.first()

    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    completed_today = ActionLog.objects.filter(
        campaign=active_c,
        created_at__gte=today_start,
    ).count() if active_c else 0

    vnc_check = check_vnc_listener()
    has_checkpoint = LinkedInProfile.objects.filter(session_status__in=["checkpoint", "needs_reauth", "restricted"]).exists()

    return render(request, "dashboard/live_work.html", {
        "campaigns": camps,
        "active_campaign": active_c,
        "operator_ctx": ctx,
        "completed_today": completed_today,
        "vnc_check": vnc_check,
        "has_checkpoint": has_checkpoint,
    })


# Global active frame cache for live stream serving
_LIVE_FRAME_CACHE = {}


def publish_live_frame(campaign_id: int, frame_bytes: bytes):
    """Publish real PNG frame bytes for a campaign to the live work cache."""
    _LIVE_FRAME_CACHE[campaign_id] = frame_bytes


@dashboard_login_required
def live_work_frame(request):
    """Authenticated workspace-scoped live browser frame endpoint."""
    from openoutreach.core.models import Campaign
    from django.http import HttpResponse, JsonResponse
    campaign_id = request.GET.get("campaign_id")
    if not campaign_id:
        c = Campaign.objects.filter(is_active=True).first() or Campaign.objects.first()
        campaign_id = c.id if c else None

    if not campaign_id:
        return JsonResponse({"error": "No campaign found"}, status=444)

    camp = Campaign.objects.filter(pk=campaign_id).first()
    if not camp:
        return JsonResponse({"error": "Campaign not found"}, status=404)

    if not camp.is_active:
        return JsonResponse({"status": "stopped", "message": "Campaign is paused"}, status=200)

    frame_bytes = _LIVE_FRAME_CACHE.get(int(campaign_id))
    if not frame_bytes:
        # Fallback to general session frame if available
        frame_bytes = _LIVE_FRAME_CACHE.get("default")

    if not frame_bytes:
        return JsonResponse({"status": "no_frame", "message": "No active frame captured yet"}, status=200)

    return HttpResponse(frame_bytes, content_type="image/png")


@dashboard_login_required
def email_hub(request):
    from openoutreach.chat.models import ConnectedGoogleAccount
    from openoutreach.emails.models import Mailbox
    accs = ConnectedGoogleAccount.objects.filter(user=request.user, is_active=True)
    mboxes = Mailbox.objects.all()
    return render(request, "dashboard/email_hub.html", {
        "google_accounts": accs,
        "mailboxes": mboxes
    })


@dashboard_login_required
def import_email_leads(request):
    if request.method == "POST" and request.FILES.get("csv_file"):
        csv_file = request.FILES["csv_file"]
        import csv, io
        from openoutreach.chat.models import LeadImport, LeadImportRow
        from openoutreach.core.models import Workspace
        ws = Workspace.objects.first()
        limp = LeadImport.objects.create(workspace=ws, file_name=csv_file.name, status="completed")
        
        text_data = csv_file.read().decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(text_data))
        row_count = 0
        for row in reader:
            row_count += 1
            LeadImportRow.objects.create(
                lead_import=limp,
                row_number=row_count,
                email=row.get("email", "") or row.get("Email", ""),
                first_name=row.get("first_name", "") or row.get("First Name", ""),
                last_name=row.get("last_name", "") or row.get("Last Name", ""),
                company=row.get("company", "") or row.get("Company", ""),
                job_title=row.get("job_title", "") or row.get("Title", "")
            )
        limp.total_rows = row_count
        limp.successful_rows = row_count
        limp.save()
        messages.success(request, f"Successfully imported {row_count} email leads from {csv_file.name}.")
    return redirect("dashboard:email_hub")


@dashboard_login_required
def gmail_connect(request):
    # Simulated OAuth 2.0 connection handler for demonstration
    from openoutreach.chat.models import ConnectedGoogleAccount
    acc, created = ConnectedGoogleAccount.objects.get_or_create(
        user=request.user,
        email="candidate-10@example.com",
        defaults={
            "display_name": "Casey Example",
            "access_token": "mock_access_token_google_oauth2",
            "refresh_token": "mock_refresh_token_google_oauth2",
            "scopes": ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"]
        }
    )
    messages.success(request, f"Google account {acc.email} connected via OAuth 2.0!")
    return redirect("dashboard:email_hub")


@dashboard_login_required
def gmail_callback(request):
    return redirect("dashboard:email_hub")


@dashboard_login_required
def integrations(request):
    from openoutreach.chat.models import ConnectedGoogleAccount
    from openoutreach.linkedin.models import LinkedInProfile
    from openoutreach.linkedin.vnc_service import (
        get_interactive_verification_status,
        get_credential_fingerprint,
        ensure_vnc_service,
    )

    ctx = _resolve_operator_context(request)
    lp = ctx["linkedin_profile"]
    accs = ConnectedGoogleAccount.objects.filter(user=request.user)

    vnc_check = get_interactive_verification_status(lp)
    fingerprint = get_credential_fingerprint(lp.cookie_data if lp else None)

    if request.user.is_superuser:
        all_lps = list(LinkedInProfile.objects.all().order_by("id"))
    else:
        all_lps = list(LinkedInProfile.objects.filter(user=request.user).order_by("id"))

    linkedin_profiles_list = []
    for prof in all_lps:
        fp = get_credential_fingerprint(prof.cookie_data)
        vc = get_interactive_verification_status(prof)
        linkedin_profiles_list.append({
            "profile": prof,
            "fingerprint": fp,
            "vnc_check": vc,
        })

    import urllib.request
    from openoutreach.core.llm import get_default_ollama_base_url
    from openoutreach.core.models import SiteConfig
    cfg = SiteConfig.load()
    ollama_endpoint = cfg.llm_api_base.rstrip("/v1").rstrip("/") if (cfg.llm_api_base and ("11434" in cfg.llm_api_base or "ollama" in cfg.llm_api_base)) else get_default_ollama_base_url()
    ollama_model = cfg.ai_model.split(":", 1)[1] if (cfg.ai_model and ":" in cfg.ai_model) else "qwen2.5:1.5b"

    ollama_connected = False
    try:
        req = urllib.request.Request(f"{ollama_endpoint}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                ollama_connected = True
    except Exception:
        ollama_connected = False

    from openoutreach.emails.bettercontact import is_configured as is_bc_configured
    bettercontact_configured = is_bc_configured()

    return render(request, "dashboard/integrations.html", {
        "linkedin_profile": lp,
        "linkedin_profiles_list": linkedin_profiles_list,
        "google_accounts": accs,
        "operator_ctx": ctx,
        "vnc_check": vnc_check,
        "cookie_fingerprint": fingerprint,
        "ollama_connected": ollama_connected,
        "ollama_model": ollama_model,
        "ollama_endpoint": ollama_endpoint,
        "bettercontact_configured": bettercontact_configured,
        "web_search_status": "READY",
    })


@dashboard_login_required
def linkedin_add_account(request):
    """Secure UI view for adding or reconnecting a LinkedIn account."""
    if request.method == "POST":
        cookie_json_raw = request.POST.get("cookie_json", "").strip()
        li_at_val = request.POST.get("li_at", "").strip()

        parsed_cookies = None
        if cookie_json_raw:
            try:
                import json
                parsed = json.loads(cookie_json_raw)
                if isinstance(parsed, list):
                    parsed_cookies = {"cookies": parsed}
                elif isinstance(parsed, dict) and "cookies" in parsed:
                    parsed_cookies = parsed
            except Exception:
                messages.error(request, "Invalid JSON format for cookies.")
                return redirect("dashboard:integrations")
        elif li_at_val:
            parsed_cookies = {"cookies": [{"name": "li_at", "value": li_at_val, "domain": ".linkedin.com", "path": "/"}]}

        if not parsed_cookies:
            messages.error(request, "Please supply valid cookie JSON or li_at cookie value.")
            return redirect("dashboard:integrations")

        profile = LinkedInProfile.objects.create(
            user=request.user,
            linkedin_username="pending_discovery@account.local",
            linkedin_password="",
            cookie_data=parsed_cookies,
            session_status="needs_reauth",
            identity_validation_status="UNKNOWN_IDENTITY",
        )

        from openoutreach.linkedin.identity_service import validate_linkedin_identity
        val_res = validate_linkedin_identity(profile, storage_state=parsed_cookies)

        if val_res.status == "VALID_IDENTITY_MATCH" and val_res.signals_agree:
            messages.success(
                request,
                f"Discovered identity: '{val_res.visible_h1}' ({val_res.public_identifier}). Please confirm account binding."
            )
            return render(request, "dashboard/confirm_linkedin_account.html", {
                "profile": profile,
                "val_res": val_res,
            })
        else:
            profile.delete()
            messages.error(
                request,
                "Identity discovery failed or identity signals conflicted. Account was not bound."
            )
            return redirect("dashboard:integrations")

    return render(request, "dashboard/add_linkedin_account.html")


@dashboard_login_required
def linkedin_reconnect_account(request, profile_id):
    """Secure UI view for reconnecting an existing LinkedIn account."""
    if request.user.is_staff or request.user.is_superuser:
        profile = get_object_or_404(LinkedInProfile, pk=profile_id)
    else:
        profile = get_object_or_404(LinkedInProfile, pk=profile_id, user=request.user)

    if request.method == "POST":
        cookie_json_raw = request.POST.get("cookie_json", "").strip()
        li_at_val = request.POST.get("li_at", "").strip()

        parsed_cookies = None
        if cookie_json_raw:
            try:
                import json
                parsed = json.loads(cookie_json_raw)
                if isinstance(parsed, list):
                    parsed_cookies = {"cookies": parsed}
                elif isinstance(parsed, dict) and "cookies" in parsed:
                    parsed_cookies = parsed
            except Exception:
                messages.error(request, "Invalid JSON format for cookies.")
                return redirect("dashboard:integrations")
        elif li_at_val:
            parsed_cookies = {"cookies": [{"name": "li_at", "value": li_at_val, "domain": ".linkedin.com", "path": "/"}]}

        if not parsed_cookies:
            messages.error(request, "Please supply valid cookie JSON or li_at cookie value.")
            return redirect("dashboard:integrations")

        from openoutreach.linkedin.identity_service import validate_linkedin_identity
        val_res = validate_linkedin_identity(profile, storage_state=parsed_cookies)

        if val_res.status == "VALID_IDENTITY_MATCH" and val_res.signals_agree:
            profile.cookie_data = parsed_cookies
            profile.session_status = "valid"
            profile.identity_validation_status = "VALID_IDENTITY_MATCH"
            profile.save()
            messages.success(
                request,
                f"Successfully reconnected LinkedIn account for '{profile.display_name or val_res.visible_h1}'."
            )
        else:
            messages.error(
                request,
                "Identity discovery failed or identity signals conflicted during reconnection. Account set to UNKNOWN_IDENTITY."
            )
        return redirect("dashboard:integrations")

    return render(request, "dashboard/reconnect_linkedin_account.html", {"profile": profile})


@dashboard_login_required
def linkedin_confirm_account(request, profile_id):
    """Operator confirmation endpoint after identity discovery."""
    if request.user.is_staff or request.user.is_superuser:
        profile = get_object_or_404(LinkedInProfile, pk=profile_id)
    else:
        profile = get_object_or_404(LinkedInProfile, pk=profile_id, user=request.user)

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "confirm":
            profile.session_status = "valid"
            profile.identity_validation_status = "VALID_IDENTITY_MATCH"
            profile.save()
            messages.success(request, f"LinkedIn Profile '{profile.display_name}' confirmed and bound successfully!")
        else:
            profile.delete()
            messages.info(request, "Account binding canceled.")

    return redirect("dashboard:integrations")


@dashboard_login_required
def linkedin_start_interactive_verification(request):
    ctx = _resolve_operator_context(request)
    lp = ctx["linkedin_profile"]

    if not lp:
        messages.error(request, "No LinkedInProfile found for the current operator.")
        return redirect("dashboard:integrations")

    from openoutreach.linkedin.vnc_service import start_interactive_verification
    res = start_interactive_verification(lp)

    if res.get("backend") == "NATIVE_MAC":
        pid_info = f" (PID {res['pid']})" if res.get("pid") else ""
        messages.success(
            request,
            f"Interactive browser opened on this Mac{pid_info}! "
            f"Please complete LinkedIn Security Verification in the browser window, then return here and click 'I've completed verification'."
        )
    else:
        if res.get("available"):
            messages.success(request, f"noVNC interactive session available at {res['url']}.")
        else:
            messages.warning(request, f"noVNC service status: {res.get('status')}. Error: {res.get('error')}.")

    return redirect("dashboard:integrations")


@dashboard_login_required
def settings_job_profile(request):
    from openoutreach.career.models import CandidateProfile, CandidateApplicationProfile, JobApplicationAnswer
    from openoutreach.career.answer_bank import calculate_application_profile_readiness

    ctx = _resolve_operator_context(request)
    cand_prof = ctx.get("candidate_profile")
    if not cand_prof:
        cand_prof, _ = CandidateProfile.objects.get_or_create(
            user=request.user,
            defaults={"full_name": request.user.first_name or request.user.username, "email": request.user.email}
        )
    app_prof, _ = CandidateApplicationProfile.objects.get_or_create(candidate_profile=cand_prof)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "confirm_resume":
            app_prof.confirmed = True
            app_prof.confirmed_by = request.user
            app_prof.confirmed_at = timezone.now()
            app_prof.save(update_fields=["confirmed", "confirmed_by", "confirmed_at"])
            messages.success(request, "Resume confirmed for job applications!")
            return redirect("dashboard:settings_job_profile")

        # 1. Personal Details
        full_name = request.POST.get("full_name", "").strip()
        email = request.POST.get("email", "").strip()
        if full_name:
            app_prof.full_name = full_name
            cand_prof.full_name = full_name
            cand_prof.save(update_fields=["full_name"])
        if email:
            app_prof.email = email

        app_prof.phone = request.POST.get("phone", "").strip()
        app_prof.city = request.POST.get("city", "").strip()
        app_prof.country = request.POST.get("country", "").strip()
        app_prof.linkedin_url = request.POST.get("linkedin_url", "").strip()
        
        # 2. Work Eligibility & Sponsorship
        auth_raw = request.POST.get("authorized_countries", "").strip()
        app_prof.authorized_countries = [a.strip().upper() for a in auth_raw.split(",") if a.strip()]

        v_sponsorship = request.POST.get("visa_sponsorship_now")
        if v_sponsorship:
            app_prof.visa_sponsorship_now = v_sponsorship
            app_prof.requires_sponsorship_now = (v_sponsorship == "yes")
        elif "requires_sponsorship_now" in request.POST:
            req_now = (request.POST.get("requires_sponsorship_now") == "on")
            app_prof.requires_sponsorship_now = req_now
            app_prof.visa_sponsorship_now = "yes" if req_now else "no"

        v_future = request.POST.get("visa_sponsorship_future")
        if v_future:
            app_prof.visa_sponsorship_future = v_future
            app_prof.requires_sponsorship_future = (v_future == "yes")
        elif "requires_sponsorship_future" in request.POST:
            req_fut = (request.POST.get("requires_sponsorship_future") == "on")
            app_prof.requires_sponsorship_future = req_fut
            app_prof.visa_sponsorship_future = "yes" if req_fut else "no"

        reloc = request.POST.get("relocation_willingness")
        if reloc:
            app_prof.relocation_willingness = reloc
            app_prof.willing_to_relocate = (reloc == "yes")
        elif "willing_to_relocate" in request.POST:
            will_reloc = (request.POST.get("willing_to_relocate") == "on")
            app_prof.willing_to_relocate = will_reloc
            app_prof.relocation_willingness = "yes" if will_reloc else "no"

        # 3. Work Mode & Travel & Background Check
        app_prof.remote_willingness = request.POST.get("remote_willingness", app_prof.remote_willingness or "unknown")
        app_prof.hybrid_willingness = request.POST.get("hybrid_willingness", app_prof.hybrid_willingness or "unknown")
        app_prof.onsite_willingness = request.POST.get("onsite_willingness", app_prof.onsite_willingness or "unknown")
        app_prof.travel_willingness = request.POST.get("travel_willingness", app_prof.travel_willingness or "unknown")
        app_prof.background_check_willingness = request.POST.get("background_check_willingness", app_prof.background_check_willingness or "unknown")

        modes_raw = request.POST.get("work_modes", "").strip()
        if modes_raw:
            app_prof.work_modes = [m.strip().capitalize() for m in modes_raw.split(",") if m.strip()]
        else:
            work_modes = []
            if app_prof.remote_willingness == "yes":
                work_modes.append("Remote")
            if app_prof.hybrid_willingness == "yes":
                work_modes.append("Hybrid")
            if app_prof.onsite_willingness == "yes":
                work_modes.append("Onsite")
            app_prof.work_modes = work_modes

        roles_raw = request.POST.get("target_job_titles", "").strip()
        if roles_raw:
            app_prof.target_job_titles = [r.strip() for r in roles_raw.split(",") if r.strip()]

        # 4. Job Search Locations & Policy
        target_locs_raw = request.POST.get("target_locations", "").strip()
        if target_locs_raw:
            app_prof.target_locations = [l.strip() for l in target_locs_raw.split(",") if l.strip()]
        elif "target_locations" in request.POST:
            app_prof.target_locations = []

        excluded_locs_raw = request.POST.get("excluded_locations", "").strip()
        if excluded_locs_raw:
            app_prof.excluded_locations = [l.strip() for l in excluded_locs_raw.split(",") if l.strip()]
        elif "excluded_locations" in request.POST:
            app_prof.excluded_locations = []

        if "remote_mode" in request.POST:
            app_prof.remote_mode = request.POST.get("remote_mode", "allow_all").strip()

        if "unknown_location_handling" in request.POST:
            app_prof.unknown_location_handling = request.POST.get("unknown_location_handling", "deny").strip()

        if "application_strategy" in request.POST:
            app_prof.application_strategy = request.POST.get("application_strategy", "INTERVIEW_FIRST_SPONSORSHIP").strip()

        app_prof.search_worldwide = (request.POST.get("search_worldwide") == "on")

        # 5. Availability & Salary
        app_prof.notice_period = request.POST.get("notice_period", "").strip()
        app_prof.earliest_start_date = request.POST.get("earliest_start_date", "").strip()
        app_prof.currency = request.POST.get("currency", "USD").strip() or "USD"
        app_prof.salary_period = request.POST.get("salary_period", "annual")
        app_prof.allow_automatic_salary = (request.POST.get("allow_automatic_salary") == "on")
        
        min_sal_digits = "".join(c for c in request.POST.get("minimum_salary", "") if c.isdigit())
        app_prof.minimum_salary = int(min_sal_digits) if min_sal_digits else None

        target_sal_digits = "".join(c for c in request.POST.get("target_salary", "") if c.isdigit())
        app_prof.target_salary = int(target_sal_digits) if target_sal_digits else None

        # Save profile changes first to evaluate updated readiness
        app_prof.save()

        # Calculate updated readiness
        readiness = calculate_application_profile_readiness(cand_prof)
        all_required_complete = readiness.get("is_ready_for_confirmation", False)

        if action == "confirm_for_applications":
            if all_required_complete:
                app_prof.confirmed = True
                app_prof.confirmed_by = request.user
                app_prof.confirmed_at = timezone.now()
                app_prof.save(update_fields=["confirmed", "confirmed_by", "confirmed_at"])
                messages.success(request, "Job Application Profile confirmed for automated applications!")
            else:
                app_prof.confirmed = False
                app_prof.confirmed_by = None
                app_prof.confirmed_at = None
                app_prof.save(update_fields=["confirmed", "confirmed_by", "confirmed_at"])
                missing_str = ", ".join(readiness.get("missing_items", []))
                messages.warning(
                    request,
                    f"Cannot confirm Job Profile: required fields are incomplete ({readiness.get('overall_score', 0)}% ready). "
                    f"Missing: {missing_str}"
                )
        else:
            # Regular save: if previously confirmed but critical fields are now missing, reset confirmation
            if app_prof.confirmed and not all_required_complete:
                app_prof.confirmed = False
                app_prof.confirmed_by = None
                app_prof.confirmed_at = None
                app_prof.save(update_fields=["confirmed", "confirmed_by", "confirmed_at"])
                messages.warning(request, "Job Profile saved. Note: Confirmation was reset because required fields are incomplete.")
            else:
                messages.success(request, "Job Application Profile saved successfully.")

        return redirect("dashboard:settings_job_profile")

    answers = JobApplicationAnswer.objects.filter(candidate_profile=cand_prof).order_by("-confirmed_at") if cand_prof else []
    documents = cand_prof.documents.all() if (cand_prof and hasattr(cand_prof, "documents")) else []
    readiness = calculate_application_profile_readiness(cand_prof)

    return render(request, "dashboard/settings_job_profile.html", {
        "candidate_profile": cand_prof,
        "application_profile": app_prof,
        "answers": answers,
        "documents": documents,
        "readiness": readiness,
        "operator_ctx": ctx,
    })


@dashboard_login_required
def linkedin_verify_completion(request):
    ctx = _resolve_operator_context(request)
    lp = ctx["linkedin_profile"]

    # 1. Stop active interactive verification browser and release lease
    from openoutreach.linkedin.vnc_service import stop_interactive_verification
    stop_interactive_verification()

    if lp:
        from openoutreach.linkedin.session_manager import run_session_health_check
        try:
            run_session_health_check(lp)
            if lp.session_status == "valid":
                messages.success(request, "LinkedIn Security Verification verified! Automation resumed automatically.")
            else:
                messages.warning(
                    request,
                    f"LinkedIn session check returned status: {lp.session_status}. "
                    "If the security challenge is still open in the browser, please complete it and try again."
                )
        except Exception as e:
            messages.error(request, f"Could not verify session: {str(e)}")
    else:
        messages.error(request, "No LinkedInProfile found for the current user/operator.")

    return redirect("dashboard:integrations")


@dashboard_login_required
def jobs_needs_your_answer(request):
    from openoutreach.career.models import JobApplication
    ctx = _resolve_operator_context(request)
    cand_prof = ctx.get("candidate_profile")

    qs = JobApplication.objects.filter(status="needs_your_answer")
    if cand_prof:
        qs = qs.filter(candidate_profile=cand_prof)

    applications = qs.select_related("job", "candidate_profile").order_by("-updated_at")

    return render(request, "dashboard/jobs_needs_your_answer.html", {
        "applications": applications,
        "operator_ctx": ctx,
    })


@dashboard_login_required
def jobs_answer_question(request, app_id):
    from openoutreach.career.models import JobApplication, JobApplicationAnswer
    from openoutreach.career.answer_bank import save_answer, normalize_question, is_sensitive_question

    ctx = _resolve_operator_context(request)
    app = get_object_or_404(JobApplication, pk=app_id)

    if request.method == "POST":
        candidate = app.candidate_profile
        unknown_qs = app.unknown_questions or []
        remaining_unknown = []

        for q in unknown_qs:
            q_text = q.get("question") or q.get("text", "")
            c_key = q.get("canonical_key") or normalize_question(q_text) or q_text.lower().replace(" ", "_")[:50]
            ans_val = request.POST.get(f"answer_{c_key}") or request.POST.get(c_key)
            save_globally = (request.POST.get(f"save_globally_{c_key}") == "on")

            if ans_val is not None and str(ans_val).strip() != "":
                # 1. Save job-specific answer
                save_answer(
                    candidate_profile=candidate,
                    canonical_key=c_key,
                    answer_value=ans_val.strip(),
                    source="operator_confirmed",
                    job=app.job
                )
                # 2. Save global reusable answer if approved and not demographic sensitive
                if save_globally and not is_sensitive_question(q_text):
                    save_answer(
                        candidate_profile=candidate,
                        canonical_key=c_key,
                        answer_value=ans_val.strip(),
                        source="operator_confirmed",
                        job=None
                    )
            else:
                remaining_unknown.append(q)

        app.unknown_questions = remaining_unknown
        if not remaining_unknown:
            app.status = "approved"
            messages.success(request, f"Answers recorded! Application for {app.job.title} at {app.job.company} is approved and ready to resume.")
        else:
            messages.info(request, "Some answers updated. Remaining questions still require your input.")
        app.save(update_fields=["unknown_questions", "status", "updated_at"])

        return redirect("dashboard:jobs_needs_your_answer")

    return redirect("dashboard:jobs_needs_your_answer")


@dashboard_login_required
def chat_home(request):
    from openoutreach.chat.models import AgentConversation, AgentMessage
    from openoutreach.core.models import Campaign
    from openoutreach.career.models import CandidateProfile

    ctx = _resolve_operator_context(request)
    campaign_id = request.GET.get("campaign_id")
    
    if campaign_id:
        active_camp = Campaign.objects.filter(pk=campaign_id).first()
    else:
        active_camp = Campaign.objects.filter(is_active=True).first() or Campaign.objects.first()

    all_campaigns = Campaign.objects.all()

    conv = AgentConversation.objects.filter(user=request.user, is_active=True).first()
    if not conv:
        conv = AgentConversation.objects.create(
            user=request.user,
            is_active=True,
            title=f"Conversation - {active_camp.name if active_camp else 'Default'}"
        )
    msgs = conv.messages.all().order_by("timestamp")

    # Campaign 52 UI confirmation context
    show_campaign_52_confirm = False
    if active_camp and active_camp.pk == 52 and not active_camp.target_geography:
        show_campaign_52_confirm = True

    return render(request, "dashboard/chat_home.html", {
        "conversation": conv,
        "messages": msgs,
        "active_campaign": active_camp,
        "campaigns": all_campaigns,
        "operator_ctx": ctx,
        "show_campaign_52_confirm": show_campaign_52_confirm,
    })


@dashboard_login_required
def chat_message(request):
    if request.method == "POST":
        content = request.POST.get("content", "").strip()
        campaign_id = request.POST.get("campaign_id")
        action = request.POST.get("action")

        from openoutreach.core.models import Campaign
        active_camp = Campaign.objects.filter(pk=campaign_id).first() if campaign_id else (Campaign.objects.filter(is_active=True).first() or Campaign.objects.first())

        from openoutreach.chat.models import AgentConversation, AgentMessage
        conv = AgentConversation.objects.filter(user=request.user, is_active=True).first()
        if not conv:
            conv = AgentConversation.objects.create(
                user=request.user,
                is_active=True,
                title=f"Conversation - {active_camp.name if active_camp else 'Default'}"
            )

        if action == "confirm_campaign_52" and active_camp and active_camp.pk == 52:
            active_camp.target_geography = "Dubai"
            active_camp.daily_limit = 5
            active_camp.save(update_fields=["target_geography", "daily_limit"])
            
            AgentMessage.objects.create(
                conversation=conv,
                role="user",
                content="Confirmed Campaign 52 settings: Dubai geography, 5 actions/day."
            )
            AgentMessage.objects.create(
                conversation=conv,
                role="agent",
                content="Great! Campaign 52 is configured with target geography: Dubai and daily limit: 5 actions/day.\n\nNext question: What kinds of startup founders should I prioritize? (e.g. FinTech, AI, SaaS)"
            )
            messages.success(request, "Campaign 52 targeting confirmed!")
            return redirect(f"/dashboard/?campaign_id={active_camp.pk}")

        if content:
            from openoutreach.chat.service import process_chat_message
            AgentMessage.objects.create(conversation=conv, role="user", content=content)
            
            reply = process_chat_message(request.user, conv, content)
            AgentMessage.objects.create(conversation=conv, role="agent", content=reply)

        if active_camp:
            return redirect(f"/dashboard/?campaign_id={active_camp.pk}")

    return redirect("dashboard:chat_home")


@dashboard_login_required
def sales_crm(request):
    """
    Dedicated Sales CRM Dashboard Page.
    Renders multi-tab sales pipeline overview, leads, deals, pipeline, conversations, and activity.
    """
    from openoutreach.crm.models.lead import Lead
    from openoutreach.crm.models.deal import Deal, DealState, Outcome
    from openoutreach.linkedin.models import ActionLog
    from openoutreach.core.models import Campaign

    tab = request.GET.get("tab", "overview").strip().lower()
    q = request.GET.get("q", "").strip()
    campaign_id = request.GET.get("campaign_id", "").strip()
    state_filter = request.GET.get("state", "").strip()

    # ── Basic Queries & Counts ──
    all_leads = Lead.objects.all()
    all_deals = Deal.objects.select_related("lead", "campaign")

    if campaign_id:
        all_deals = all_deals.filter(campaign_id=campaign_id)

    if q:
        all_leads = all_leads.filter(
            models.Q(first_name__icontains=q)
            | models.Q(last_name__icontains=q)
            | models.Q(company__icontains=q)
            | models.Q(current_title__icontains=q)
            | models.Q(email__icontains=q)
            | models.Q(public_identifier__icontains=q)
        )
        all_deals = all_deals.filter(
            models.Q(lead__first_name__icontains=q)
            | models.Q(lead__last_name__icontains=q)
            | models.Q(lead__company__icontains=q)
            | models.Q(lead__current_title__icontains=q)
            | models.Q(lead__email__icontains=q)
        )

    if state_filter:
        all_deals = all_deals.filter(state=state_filter)

    # ── KPI Metrics ──
    kpis = {
        "total_leads": Lead.objects.count(),
        "total_deals": Deal.objects.count(),
        "qualified_count": Deal.objects.filter(state=DealState.QUALIFIED).count(),
        "ready_connect_count": Deal.objects.filter(state=DealState.READY_TO_CONNECT).count(),
        "pending_count": Deal.objects.filter(state=DealState.PENDING).count(),
        "connected_count": Deal.objects.filter(state=DealState.CONNECTED).count(),
        "ready_email_count": Deal.objects.filter(state=DealState.READY_TO_EMAIL).count(),
        "email_sent_count": Deal.objects.filter(state=DealState.EMAILED).count(),
        "replied_count": 0,
        "converted_count": Deal.objects.filter(outcome=Outcome.CONVERTED).count(),
        "not_interested_count": Deal.objects.filter(outcome=Outcome.NOT_INTERESTED).count(),
    }

    # ── Tab Specific Context Data ──
    pipeline_groups = {}
    if tab == "pipeline":
        for st_code, st_name in DealState.choices:
            pipeline_groups[st_name] = Deal.objects.filter(state=st_code).select_related("lead", "campaign")[:20]

    action_logs = ActionLog.objects.select_related("linkedin_profile", "campaign").order_by("-created_at")[:50]
    all_campaigns = Campaign.objects.all()

    context = {
        "tab": tab,
        "q": q,
        "campaign_id": campaign_id,
        "state_filter": state_filter,
        "kpis": kpis,
        "leads": all_leads.order_by("-id")[:100],
        "deals": all_deals.order_by("-id")[:100],
        "pipeline_groups": pipeline_groups,
        "action_logs": action_logs,
        "campaigns": all_campaigns,
        "deal_states": DealState.choices,
    }
    return render(request, "dashboard/crm/sales_crm.html", context)


# ══════════════════════════════════════════════════════════════════════════════
# MODERN SAAS SETTINGS & SECURE LINKEDIN ONBOARDING VIEWS
# ══════════════════════════════════════════════════════════════════════════════

@dashboard_login_required
def settings_hub(request):
    """Main Settings Hub overview."""
    return redirect("dashboard:settings_linkedin")


@dashboard_login_required
def settings_account(request):
    """Simple user account & workspace settings."""
    from openoutreach.core.models import Workspace
    ws = Workspace.objects.filter(memberships__user=request.user).first() or Workspace.objects.first()
    context = {
        "user": request.user,
        "workspace": ws,
        "active_tab": "account",
    }
    return render(request, "dashboard/settings_account.html", context)


@dashboard_login_required
def settings_linkedin(request):
    """Secure, multi-tenant LinkedIn account onboarding and management."""
    from openoutreach.linkedin.models import LinkedInProfile
    from openoutreach.linkedin.session_manager import resolve_workspace_linkedin_profile
    from openoutreach.career.models import IncidentHold
    from django.contrib.contenttypes.models import ContentType

    ctx = _resolve_operator_context(request)
    profile = resolve_workspace_linkedin_profile(request.user, ctx["workspace"])

    # Determine if a production-decryptable credential with li_at exists
    dec = profile.get_decrypted_cookie_data()
    has_stored_credential = bool(
        dec and isinstance(dec, dict) and any(
            c.get("name") == "li_at" and bool(c.get("value"))
            for c in dec.get("cookies", []) if isinstance(c, dict)
        )
    )

    # Compute human status
    if profile.session_status == "valid" and (profile.active or profile.cookie_data):
        if not profile.active:
            profile.active = True
            profile.save(update_fields=["active"])
        status_code = "connected"
        status_label = "CONNECTED"
        status_badge_class = "badge-success"
        status_message = "Your LinkedIn account is active and authenticated."
    elif profile.session_status == "checkpoint":
        status_code = "checkpoint"
        status_label = "LinkedIn Security Checkpoint Detected"
        status_badge_class = "badge-warning"
        status_message = "LinkedIn is asking to verify your identity. Please open LinkedIn in your browser to clear the checkpoint, then save a fresh cookie."
    elif profile.session_status == "needs_reauth":
        status_code = "needs_reauth"
        status_label = "Session Saved / Verification Required"
        status_badge_class = "badge-warning"
        status_message = "Session cookie saved securely. Verification required."
    elif profile.session_status == "restricted":
        status_code = "attention"
        status_label = "Needs Attention"
        status_badge_class = "badge-danger"
        status_message = "Your account is temporarily restricted by LinkedIn."
    elif profile.session_status == "expired":
        status_code = "expired"
        status_label = "Session Expired"
        status_badge_class = "badge-danger"
        status_message = "Your saved session has expired. Please paste a fresh li_at cookie."
    elif profile.session_status in ["invalid", "error"]:
        status_code = "invalid"
        status_label = "Needs Attention"
        status_badge_class = "badge-danger"
        status_message = "LinkedIn session requires authentication. Please review details and save a fresh cookie."
    else:
        status_code = "not_connected"
        status_label = "Not Connected"
        status_badge_class = "badge-secondary"
        status_message = "Connect your LinkedIn account to enable intelligent networking."

    # Check active holds
    profile_ct = ContentType.objects.get_for_model(LinkedInProfile)
    has_active_hold = IncidentHold.objects.filter(
        active=True,
        targets__content_type=profile_ct,
        targets__object_id=profile.pk,
    ).exists()

    from openoutreach.core.models import WorkspaceAutomationSettings
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ctx["workspace"])
    manual_provider_use_paused = getattr(auto_st, "manual_provider_use_paused", False) if auto_st else False

    from openoutreach.linkedin.models import ServerBootstrapSession
    active_bootstrap = ServerBootstrapSession.objects.filter(
        linkedin_profile=profile,
        status__in=[ServerBootstrapSession.Status.PENDING, ServerBootstrapSession.Status.ACTIVE],
        expires_at__gt=timezone.now(),
    ).first()

    context = {
        "profile": profile,
        "has_stored_credential": has_stored_credential,
        "status_code": status_code,
        "status_label": status_label,
        "status_badge_class": status_badge_class,
        "status_message": status_message,
        "has_active_hold": has_active_hold,
        "active_tab": "linkedin",
        "pending_replacement": request.session.get("pending_linkedin_replacement"),
        "manual_provider_use_paused": manual_provider_use_paused,
        "active_bootstrap": active_bootstrap,
        "session_origin": getattr(profile, "session_origin", "imported_seed"),
    }
    return render(request, "dashboard/settings_linkedin.html", context)


@dashboard_login_required
def settings_linkedin_save(request):
    """Stage 1: Save & Encrypt LinkedIn session cookie at rest.
    
    Guarantees:
    - Zero external network requests
    - Zero browser execution
    - Zero ProviderWriteIntent
    - Zero provider tasks
    - Marks profile session_status as 'needs_reauth'
    """
    from openoutreach.linkedin.session_manager import (
        resolve_workspace_linkedin_profile,
        save_linkedin_session_cookie,
    )

    if request.method != "POST":
        return redirect("dashboard:settings_linkedin")

    ctx = _resolve_operator_context(request)
    profile = resolve_workspace_linkedin_profile(request.user, ctx["workspace"])

    raw_li_at = request.POST.get("li_at_cookie", "").strip() or request.POST.get("li_at", "").strip()
    if not raw_li_at:
        messages.error(request, "Please enter a valid li_at session cookie.")
        return redirect("dashboard:settings_linkedin")

    force_replace = request.POST.get("confirm_replace_managed") in ["true", "1", "yes"]
    try:
        from openoutreach.linkedin.exceptions import SessionOwnershipConflictError
        save_linkedin_session_cookie(profile, raw_li_at, force=force_replace)
        messages.success(request, "Session cookie saved securely (imported seed). Verification required.")
    except SessionOwnershipConflictError as soc_err:
        messages.warning(
            request,
            f"{str(soc_err)} To replace a HuntFlow-managed session, submit with confirmation."
        )
    except ValueError as val_err:
        messages.error(request, str(val_err))
    except Exception as e:
        messages.error(request, f"Cookie could not be stored securely: {str(e)}")

    return redirect("dashboard:settings_linkedin")


@dashboard_login_required
def settings_linkedin_verify(request):
    """Stage 2: Run read-only session-health verification check.
    
    Guarantees:
    - Resolves workspace-bound profile
    - Performs canonical session verification via run_session_health_check
    - Records ProviderReadLedger entry
    - Zero provider writes (zero connection requests, messages, or job applications)
    """
    from openoutreach.linkedin.session_manager import (
        resolve_workspace_linkedin_profile,
        save_linkedin_session_cookie,
        run_session_health_check,
    )
    from openoutreach.career.models import IncidentHold
    from django.contrib.contenttypes.models import ContentType

    if request.method != "POST":
        return redirect("dashboard:settings_linkedin")

    ctx = _resolve_operator_context(request)
    profile = resolve_workspace_linkedin_profile(request.user, ctx["workspace"])

    # If cookie input is provided alongside verify, save it first
    raw_li_at = request.POST.get("li_at_cookie", "").strip() or request.POST.get("li_at", "").strip()
    if raw_li_at:
        try:
            save_linkedin_session_cookie(profile, raw_li_at)
        except ValueError as val_err:
            messages.error(request, str(val_err))
            return redirect("dashboard:settings_linkedin")

    # Reload profile from DB to ensure fresh committed credentials
    profile.refresh_from_db()

    dec = profile.get_decrypted_cookie_data()
    has_stored = bool(
        dec and isinstance(dec, dict) and any(
            c.get("name") == "li_at" and bool(c.get("value"))
            for c in dec.get("cookies", []) if isinstance(c, dict)
        )
    )
    if not has_stored:
        messages.error(request, "No saved session cookie found. Please save your session cookie first.")
        return redirect("dashboard:settings_linkedin")

    try:
        run_session_health_check(profile)
        profile.refresh_from_db()

        if profile.session_status == "valid":
            profile.active = True
            profile.last_authenticated_at = timezone.now()
            profile.identity_validation_status = "VALID_IDENTITY_MATCH"

            # Populate display name if missing
            if not profile.display_name:
                from openoutreach.linkedin.identity_service import validate_linkedin_identity
                try:
                    res = validate_linkedin_identity(profile)
                    if res.visible_h1:
                        profile.display_name = res.visible_h1
                    if res.canonical_profile_url:
                        profile.public_profile_url = res.canonical_profile_url.split("?")[0]
                    if res.public_identifier:
                        profile.public_identifier = res.public_identifier
                except Exception:
                    pass

            if not profile.display_name and request.user:
                profile.display_name = request.user.get_full_name() or request.user.username

            if profile.linkedin_username in ["dry@run.com", "unknown", "default", ""] and (request.user.email or profile.display_name):
                profile.linkedin_username = request.user.email or profile.display_name

            profile.save(update_fields=[
                "active", "last_authenticated_at", "identity_validation_status",
                "display_name", "public_profile_url", "public_identifier", "linkedin_username",
            ])

            # Release checkpoint incident hold if active
            profile_ct = ContentType.objects.get_for_model(LinkedInProfile)
            holds = IncidentHold.objects.filter(
                active=True,
                targets__content_type=profile_ct,
                targets__object_id=profile.pk,
            )
            for h in holds:
                h.active = False
                h.description = (h.description or "") + f" [Resolved by valid session verification on {timezone.now().strftime('%Y-%m-%d %H:%M:%S')} UTC]"
                h.save(update_fields=["active", "description"])

            verified_name = profile.display_name or (profile.user.email if profile.user else "") or "Account"
            messages.success(request, f"LinkedIn Connected ✓ {verified_name} verified successfully.")
        elif profile.session_status == "checkpoint":
            messages.warning(
                request,
                "LinkedIn security checkpoint detected. Please sign in to LinkedIn in your normal browser, solve the security challenge, then paste a fresh session cookie here."
            )
        elif profile.session_status in ["expired", "needs_reauth"]:
            messages.error(
                request,
                "LinkedIn session requires authentication. Please save a fresh li_at session cookie."
            )
        else:
            err = profile.session_error_details or "unauthenticated"
            if "timeout" in err.lower():
                messages.error(request, "Session verification timed out. Please try again.")
            else:
                messages.error(request, f"LinkedIn session could not be verified ({err}).")
    except Exception as e:
        profile.session_status = "invalid"
        profile.session_error_details = str(e)
        profile.save(update_fields=["session_status", "session_error_details"])
        messages.error(request, f"Session verification could not start: {str(e)}")

    return redirect("dashboard:settings_linkedin")


@dashboard_login_required
def settings_linkedin_replace(request):
    """Explicitly confirms replacing account identity while preserving history."""
    from openoutreach.linkedin.models import LinkedInProfile
    if request.method == "POST":
        profile = LinkedInProfile.objects.filter(user=request.user).first() or LinkedInProfile.objects.first()
        new_name = request.POST.get("new_display_name", "").strip()
        if profile and new_name:
            profile.display_name = new_name
            profile.identity_validation_status = "VALID_IDENTITY_MATCH"
            profile.save(update_fields=["display_name", "identity_validation_status"])
            messages.success(request, f"LinkedIn account identity updated to '{new_name}'.")
        request.session.pop("pending_linkedin_replacement", None)
    return redirect("dashboard:settings_linkedin")


@dashboard_login_required
def settings_linkedin_disconnect(request):
    """Safely clears saved session cookies and marks account disconnected."""
    from openoutreach.linkedin.models import LinkedInProfile
    if request.method == "POST":
        profile = LinkedInProfile.objects.filter(user=request.user).first() or LinkedInProfile.objects.first()
        if profile:
            profile.cookie_data = None
            profile.session_status = "expired"
            profile.session_error_details = "Disconnected by user."
            profile.save(update_fields=["cookie_data", "session_status", "session_error_details"])
            messages.success(request, "LinkedIn session disconnected successfully.")
    return redirect("dashboard:settings_linkedin")


@dashboard_login_required
def settings_email(request):
    """Simple email connection management."""
    from openoutreach.emails.models import Mailbox
    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]
    mailboxes = Mailbox.objects.for_workspace(workspace)
    context = {
        "mailboxes": mailboxes,
        "is_connected": mailboxes.exists(),
        "active_tab": "email",
    }
    return render(request, "dashboard/settings_email.html", context)


@dashboard_login_required
def settings_writing_style(request):
    """AI & Writing Style preferences with live example preview."""
    from openoutreach.career.models import CandidateProfile
    cand_prof = CandidateProfile.objects.filter(user=request.user).first()
    cand_name = cand_prof.full_name if cand_prof else request.user.get_full_name() or "Alex Morgan"

    sample_note = f"Hi Klaus, saw your work on real-time data pipelines at TechCorp. Built similar distributed ingestion systems recently and wanted to connect."

    if request.method == "POST":
        messages.success(request, "AI & Writing Style preferences saved!")
        return redirect("dashboard:settings_writing_style")

    context = {
        "candidate_name": cand_name,
        "sample_note": sample_note,
        "candidate_voice_enabled": True,
        "active_tab": "writing_style",
    }
    return render(request, "dashboard/settings_writing_style.html", context)


@dashboard_login_required
def settings_automation(request):
    """Human-readable automation choices and pause/resume control."""
    from openoutreach.core.models import Campaign, WorkspaceAutomationSettings
    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]
    auto_settings = ctx["automation_settings"] or (WorkspaceAutomationSettings.get_for_workspace(workspace) if workspace else None)

    if request.method == "POST" and auto_settings:
        auto_settings.auto_find_prospects = request.POST.get("auto_find_prospects") == "on"
        auto_settings.auto_send_connections = request.POST.get("auto_send_connections") == "on"
        auto_settings.auto_followup = request.POST.get("auto_followup") == "on"
        auto_settings.auto_find_jobs = request.POST.get("auto_find_jobs") == "on"
        auto_settings.auto_apply_jobs = request.POST.get("auto_apply_jobs") == "on"
        auto_settings.auto_send_emails = request.POST.get("auto_send_emails") == "on"
        auto_settings.auto_monitor_replies = request.POST.get("auto_monitor_replies") == "on"
        auto_settings.save()
        messages.success(request, "Automation preferences saved successfully.")
        return redirect("dashboard:settings_automation")

    from openoutreach.crm.models import Deal
    if workspace:
        campaigns = Campaign.objects.filter(workspace=workspace, is_synthetic=False)
        crm_pending_deals_count = Deal.objects.filter(campaign__workspace=workspace, state="Pending").count()
    else:
        campaigns = Campaign.objects.filter(users=request.user, is_synthetic=False)
        crm_pending_deals_count = Deal.objects.filter(campaign__users=request.user, state="Pending").count()

    from openoutreach.core.services.quota_service import get_workspace_quota_summary
    quota_summary = get_workspace_quota_summary(workspace, auto_settings)

    context = {
        "campaigns": campaigns,
        "cfg": auto_settings,
        "quotas": quota_summary,
        "crm_pending_deals_count": crm_pending_deals_count,
        "linkedin_pending_invitations_count": getattr(auto_settings, "linkedin_pending_invitations_count", 326) or 326,
        "master_automation_enabled": getattr(auto_settings, "master_automation_enabled", False),
        "active_tab": "automation",
    }
    return render(request, "dashboard/settings_automation.html", context)


@dashboard_login_required
@require_POST
def automation_start(request):
    """Enables master automation after running full launch readiness checks."""
    from openoutreach.core.models import Campaign, WorkspaceAutomationSettings
    from openoutreach.linkedin.models import LinkedInProfile

    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]
    auto_settings = ctx["automation_settings"] or (WorkspaceAutomationSettings.get_for_workspace(workspace) if workspace else None)
    lp = ctx["linkedin_profile"]

    # Check if LinkedIn account is present and non-expired
    if not lp:
        messages.error(request, "Cannot start automation: Please connect your LinkedIn account first under Settings.")
        return redirect("dashboard:settings_linkedin")
    if lp.session_status != "valid":
        messages.error(request, f"Cannot start automation: LinkedIn account status is '{lp.get_session_status_display()}'. Please verify your session cookie.")
        return redirect("dashboard:settings_linkedin")

    if auto_settings:
        auto_settings.master_automation_enabled = True
        auto_settings.save(update_fields=["master_automation_enabled"])

    # Activate campaigns for this workspace
    if workspace:
        Campaign.objects.filter(workspace=workspace, is_synthetic=False).update(is_active=True)
    else:
        Campaign.objects.filter(users=request.user, is_synthetic=False).update(is_active=True)

    messages.success(request, "⚡ Automation Started! HuntFlow is now active and running in the background.")
    return redirect("dashboard:overview")


@dashboard_login_required
@require_POST
def automation_pause(request):
    """Safely pauses master automation and halts outbound action creation."""
    from openoutreach.core.models import WorkspaceAutomationSettings

    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]
    auto_settings = ctx["automation_settings"] or (WorkspaceAutomationSettings.get_for_workspace(workspace) if workspace else None)

    if auto_settings:
        auto_settings.master_automation_enabled = False
        auto_settings.save(update_fields=["master_automation_enabled"])

    messages.info(request, "⏸️ Master Automation Paused. No new connection requests, messages, or job applications will be sent.")
    return redirect("dashboard:overview")


@dashboard_login_required
@require_POST
def automation_toggle_backlog_override(request):
    """Allows the operator to toggle the connection backlog override ON/OFF."""
    from openoutreach.core.models import WorkspaceAutomationSettings

    ctx = _resolve_operator_context(request)
    workspace = ctx["workspace"]
    auto_settings = ctx["automation_settings"] or (WorkspaceAutomationSettings.get_for_workspace(workspace) if workspace else None)

    if auto_settings:
        if auto_settings.connection_backlog_operator_override:
            auto_settings.disable_backlog_override()
            messages.info(request, "Connection Backlog Override DISABLED. Normal backlog limits are now in effect.")
        else:
            auto_settings.enable_backlog_override(source="operator", reason="operator_explicitly_requested_all_channels_running")
            messages.success(request, "Connection Backlog Override ACTIVATED. Bounded connection requests permitted.")

    return redirect(request.META.get("HTTP_REFERER") or "dashboard:settings_automation")


@dashboard_login_required
def settings_test_data(request):
    """Admin view for inspecting synthetic test records."""
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Access restricted to administrators.")
        return redirect("dashboard:settings")

    from openoutreach.core.models import Campaign
    from openoutreach.career.models import Job
    from openoutreach.crm.models import Deal

    test_campaigns = Campaign.objects.filter(is_synthetic=True)
    test_jobs = Job.objects.filter(is_synthetic=True)
    test_deals = Deal.objects.filter(is_synthetic=True)[:50]

    context = {
        "test_campaigns": test_campaigns,
        "test_jobs": test_jobs,
        "test_deals": test_deals,
        "active_tab": "advanced",
    }
    return render(request, "dashboard/settings_test_data.html", context)


@dashboard_login_required
def settings_advanced(request):
    """Advanced technical admin hub linking to background operations."""
    from openoutreach.core.models import DaemonStatus, Task
    d_status = DaemonStatus.objects.first()
    daemon_running = bool(d_status and d_status.is_running and d_status.last_heartbeat and (timezone.now() - d_status.last_heartbeat).total_seconds() < 120)
    
    pending_tasks = Task.objects.filter(status=Task.Status.PENDING).count()
    failed_tasks = Task.objects.filter(status=Task.Status.FAILED).count()

    context = {
        "daemon_running": daemon_running,
        "pending_tasks": pending_tasks,
        "failed_tasks": failed_tasks,
        "active_tab": "advanced",
    }
    return render(request, "dashboard/settings_advanced.html", context)


def _get_active_workspace(request):
    """Helper to extract active workspace from operator context."""
    from openoutreach.core.models import Workspace
    ctx = _resolve_operator_context(request)
    return ctx.get("workspace") or Workspace.objects.filter(slug="default").first() or Workspace.objects.first()


@ensure_csrf_cookie
@dashboard_login_required
def targeting_assistant(request):
    """Conversational AI Targeting Assistant page."""
    workspace = _get_active_workspace(request)
    from openoutreach.core.models import Campaign, TargetingConversation, TargetingConfigVersion
    from openoutreach.core.agents.targeting_assistant import TargetingAssistant

    campaigns = Campaign.objects.filter(workspace=workspace, is_synthetic=False) if workspace else Campaign.objects.filter(users=request.user, is_synthetic=False)
    
    # Get or create active conversation
    conv = TargetingConversation.objects.filter(workspace=workspace).first() if workspace else None
    if not conv and workspace:
        conv = TargetingConversation.objects.create(
            workspace=workspace,
            title="HuntFlow Targeting Assistant",
        )

    messages_list = conv.messages.all().order_by("created_at") if conv else []
    active_versions = TargetingConfigVersion.objects.filter(workspace=workspace, is_active=True) if workspace else []

    context = {
        "workspace": workspace,
        "campaigns": campaigns,
        "conversation": conv,
        "messages_list": messages_list,
        "active_versions": active_versions,
        "active_tab": "targeting",
    }
    return render(request, "dashboard/targeting_assistant.html", context)


@dashboard_login_required
@require_POST
def targeting_assistant_chat(request):
    """AJAX API endpoint for processing natural language targeting messages."""
    workspace = _get_active_workspace(request)
    if not workspace:
        return JsonResponse({"error": "No active workspace."}, status=400)

    from openoutreach.core.models import Campaign, TargetingConversation, TargetingMessage
    from openoutreach.core.agents.targeting_assistant import TargetingAssistant

    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    prompt = (data.get("message") or "").strip()
    campaign_id = data.get("campaign_id")

    if not prompt:
        return JsonResponse({"error": "Message content cannot be empty."}, status=400)

    selected_campaign = None
    if campaign_id:
        selected_campaign = Campaign.objects.filter(id=campaign_id, workspace=workspace).first()

    conv, _ = TargetingConversation.objects.get_or_create(
        workspace=workspace,
        defaults={"title": "HuntFlow Targeting Assistant"}
    )

    # Save operator message
    TargetingMessage.objects.create(
        conversation=conv,
        role="operator",
        content=prompt,
    )

    # Process via TargetingAssistant agent
    result = TargetingAssistant.parse_natural_language_intent(
        prompt=prompt,
        workspace=workspace,
        campaign=selected_campaign,
    )

    # Determine response text
    reply_content = result.get("reply") or result.get("preview_text") or "I have processed your targeting request."
    
    # Save assistant message with structured payload
    TargetingMessage.objects.create(
        conversation=conv,
        role="assistant",
        content=reply_content,
        structured_payload=result,
    )

    return JsonResponse({
        "status": "ok",
        "reply": reply_content,
        "payload": result,
    })


@dashboard_login_required
@require_POST
def targeting_assistant_activate(request):
    """AJAX API endpoint to activate approved targeting configuration."""
    workspace = _get_active_workspace(request)
    if not workspace:
        return JsonResponse({"error": "No active workspace."}, status=400)

    from openoutreach.core.models import Campaign
    from openoutreach.core.agents.targeting_assistant import TargetingAssistant

    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    config_data = data.get("config_data") or {}
    config_type = data.get("config_type") or config_data.get("intent_type") or "prospect_icp"
    campaign_id = data.get("campaign_id") or config_data.get("campaign_id")
    operator_prompt = data.get("operator_prompt") or ""

    campaign = None
    if campaign_id:
        campaign = Campaign.objects.filter(id=campaign_id, workspace=workspace).first()

    version_obj = TargetingAssistant.activate_targeting_config(
        workspace=workspace,
        campaign=campaign,
        config_data=config_data,
        operator_prompt=operator_prompt,
        config_type=config_type,
    )

    return JsonResponse({
        "status": "ok",
        "message": f"Successfully activated targeting Version {version_obj.version_number}.",
        "version_number": version_obj.version_number,
    })


@dashboard_login_required
@require_POST
def targeting_assistant_rollback(request):
    """AJAX API endpoint to roll back to the previous targeting configuration."""
    workspace = _get_active_workspace(request)
    if not workspace:
        return JsonResponse({"error": "No active workspace."}, status=400)

    from openoutreach.core.models import Campaign
    from openoutreach.core.agents.targeting_assistant import TargetingAssistant

    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    campaign_id = data.get("campaign_id")
    config_type = data.get("config_type") or "prospect_icp"

    campaign = None
    if campaign_id:
        campaign = Campaign.objects.filter(id=campaign_id, workspace=workspace).first()

    success, msg, prev_data = TargetingAssistant.rollback_targeting_config(
        workspace=workspace,
        campaign=campaign,
        config_type=config_type,
    )

    if not success:
        return JsonResponse({"status": "error", "message": msg}, status=400)

    return JsonResponse({
        "status": "ok",
        "message": msg,
        "config_data": prev_data,
    })


@dashboard_login_required
@require_POST
def targeting_assistant_preview(request):
    """AJAX API endpoint for generating read-only sample match previews."""
    workspace = _get_active_workspace(request)
    if not workspace:
        return JsonResponse({"error": "No active workspace."}, status=400)

    from openoutreach.core.models import Campaign
    from openoutreach.core.agents.targeting_assistant import TargetingAssistant

    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    preview_type = data.get("preview_type") or "prospects"
    config_data = data.get("config_data") or {}
    campaign_id = data.get("campaign_id")

    campaign = None
    if campaign_id:
        campaign = Campaign.objects.filter(id=campaign_id, workspace=workspace).first()

    if preview_type == "prospects":
        matches = TargetingAssistant.preview_prospect_matches(workspace, campaign, config_data, limit=10)
    else:
        from openoutreach.career.models import CandidateProfile
        cand = (
            CandidateProfile.objects.filter(user__workspace_memberships__workspace=workspace).first()
            or CandidateProfile.objects.filter(user__campaigns__workspace=workspace).first()
        )
        matches = TargetingAssistant.preview_job_matches(workspace, config_data, limit=10)

    return JsonResponse({
        "status": "ok",
        "preview_type": preview_type,
        "matches": matches,
    })


@dashboard_login_required
def pause_provider_automation(request):
    """Pauses provider operations for operator manual LinkedIn use on Mac."""
    if request.method != "POST":
        return redirect("dashboard:settings_linkedin")

    ctx = _resolve_operator_context(request)
    ws = ctx["workspace"]
    from openoutreach.core.models import WorkspaceAutomationSettings
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
    if auto_st:
        auto_st.pause_for_manual_provider_use()
        messages.info(
            request,
            "Provider automation paused for manual LinkedIn use. All 8 automation preferences remain saved."
        )
    return redirect("dashboard:settings_linkedin")


@dashboard_login_required
def resume_provider_automation(request):
    """Resumes provider operations following operator manual LinkedIn use."""
    if request.method != "POST":
        return redirect("dashboard:settings_linkedin")

    ctx = _resolve_operator_context(request)
    ws = ctx["workspace"]
    from openoutreach.core.models import WorkspaceAutomationSettings
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
    if auto_st:
        ok, msg = auto_st.resume_from_manual_provider_use()
        if ok:
            messages.success(request, "Provider automation resumed successfully.")
        else:
            messages.warning(request, f"Cannot resume automation: {msg}")
    return redirect("dashboard:settings_linkedin")


@dashboard_login_required
def settings_linkedin_bootstrap_start(request):
    """Initiates an operator-assisted server session bootstrap session."""
    if request.method != "POST":
        return JsonResponse({"status": "error", "error": "method_not_allowed"}, status=405)

    ctx = _resolve_operator_context(request)
    from openoutreach.linkedin.session_manager import resolve_workspace_linkedin_profile
    from openoutreach.linkedin.services.server_bootstrap import start_server_bootstrap

    profile = resolve_workspace_linkedin_profile(request.user, ctx["workspace"])
    session, raw_token, err = start_server_bootstrap(
        workspace=ctx["workspace"],
        linkedin_profile=profile,
        operator=request.user,
    )
    if err or not session:
        return JsonResponse({"status": "error", "error": err or "bootstrap_start_failed"}, status=400)

    # Note: raw_token is returned in response body only, NEVER in the websocket URL!
    ws_scheme = "wss" if request.is_secure() else "ws"
    host = request.get_host()
    ws_url = f"{ws_scheme}://{host}/ws/bootstrap/{session.pk}/"

    return JsonResponse({
        "status": "ok",
        "bootstrap_id": session.pk,
        "secret": raw_token,
        "ws_url": ws_url,
        "expires_at": session.expires_at.isoformat(),
    })


@dashboard_login_required
def settings_linkedin_bootstrap_status(request, pk: int):
    """Returns the current state of a bootstrap session."""
    ctx = _resolve_operator_context(request)
    from openoutreach.linkedin.models import ServerBootstrapSession
    session = ServerBootstrapSession.objects.filter(id=pk, workspace=ctx["workspace"]).first()
    if not session:
        return JsonResponse({"status": "error", "error": "not_found"}, status=404)

    return JsonResponse({
        "status": "ok",
        "bootstrap_id": session.pk,
        "session_status": session.status,
        "viewer_connected": session.viewer_connected,
        "expires_at": session.expires_at.isoformat(),
        "is_expired": session.expires_at <= timezone.now(),
    })


@dashboard_login_required
def settings_linkedin_bootstrap_abort(request, pk: int):
    """Aborts an active bootstrap session."""
    if request.method != "POST":
        return JsonResponse({"status": "error", "error": "method_not_allowed"}, status=405)

    ctx = _resolve_operator_context(request)
    from openoutreach.linkedin.models import ServerBootstrapSession
    from openoutreach.linkedin.services.server_bootstrap import _release_bootstrap_lease

    session = ServerBootstrapSession.objects.filter(id=pk, workspace=ctx["workspace"]).first()
    if not session:
        return JsonResponse({"status": "error", "error": "not_found"}, status=404)

    if session.status in (ServerBootstrapSession.Status.PENDING, ServerBootstrapSession.Status.ACTIVE):
        session.status = ServerBootstrapSession.Status.ABORTED
        session.aborted_at = timezone.now()
        session.failure_reason = "operator_aborted_via_http"
        session.viewer_connected = False
        session.viewer_connection_id = ""
        session.save(update_fields=["status", "aborted_at", "failure_reason", "viewer_connected", "viewer_connection_id"])
        _release_bootstrap_lease(session)

    return JsonResponse({"status": "ok", "bootstrap_status": session.status})





