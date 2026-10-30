# openoutreach/core/models.py
from __future__ import annotations

import logging
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator

logger = logging.getLogger(__name__)


PLACEHOLDER_KEY_PATTERNS = (
    "ROTATED_",
    "PLACEHOLDER",
    "SENTINEL",
    "YOUR_KEY",
    "CHANGEME",
    "REDACTED",
    "SECRETS",
    "CONFIGURED",
    "MISSING",
    "INVALID",
    "UNKNOWN",
    "DUMMY",
    "XXX",
)


def is_valid_credential(val: str) -> bool:
    """Return True if val is a non-empty, non-placeholder credential string."""
    if not val or not val.strip():
        return False
    val_upper = val.strip().upper()
    for pattern in PLACEHOLDER_KEY_PATTERNS:
        if pattern in val_upper:
            return False
    if len(val.strip()) < 8:
        return False
    return True


def get_credential_state(val: str) -> str:
    """Return credential status classification: CONFIGURED, MISSING, PLACEHOLDER, ROTATION_REQUIRED, or INVALID."""
    if not val or not val.strip():
        return "MISSING"
    val_upper = val.strip().upper()
    if val_upper.startswith("ROTATED_") or "ROTATED" in val_upper:
        return "ROTATION_REQUIRED"
    for pattern in PLACEHOLDER_KEY_PATTERNS:
        if pattern in val_upper:
            return "PLACEHOLDER"
    if len(val.strip()) < 8:
        return "INVALID"
    return "CONFIGURED"


class SiteConfig(models.Model):
    """Singleton model for global site configuration (LLM keys, etc.)."""

    ai_model = models.CharField(
        max_length=200, blank=True, default="",
        help_text="provider:model, e.g. anthropic:claude-sonnet-4-5-20250929",
    )
    llm_api_key = models.CharField(max_length=500, blank=True, default="")
    llm_api_base = models.CharField(max_length=500, blank=True, default="")

    bettercontact_api_key = models.CharField(max_length=500, blank=True, default="")

    contacts_api_token = models.CharField(max_length=500, blank=True, default="")
    contacts_api_url = models.CharField(max_length=500, blank=True, default="")

    master_automation_enabled = models.BooleanField(default=False)
    auto_find_prospects = models.BooleanField(default=True)
    auto_send_connections = models.BooleanField(default=True)
    auto_followup = models.BooleanField(default=True)
    auto_find_jobs = models.BooleanField(default=True)
    auto_apply_jobs = models.BooleanField(default=True)
    auto_send_emails = models.BooleanField(default=True)
    auto_monitor_replies = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Site Configuration"
        verbose_name_plural = "Site Configuration"

    def __str__(self):
        return "Site Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SiteConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def llm_provider(self) -> str:
        if self.ai_model and ":" in self.ai_model:
            return self.ai_model.split(":")[0].strip().lower()
        return ""

    def is_llm_configured(self) -> bool:
        if self.llm_provider == "ollama":
            return bool(self.ai_model and len(self.ai_model.strip()) > 7)
        return is_valid_credential(self.llm_api_key)

    def is_bettercontact_configured(self) -> bool:
        return is_valid_credential(self.bettercontact_api_key)

    def get_llm_credential_state(self) -> str:
        if self.llm_provider == "ollama":
            return "CONFIGURED"
        return get_credential_state(self.llm_api_key)

    def get_bettercontact_credential_state(self) -> str:
        return get_credential_state(self.bettercontact_api_key)



class TenantQuerySet(models.QuerySet):
    def for_workspace(self, workspace):
        return self.filter(workspace=workspace)


class Campaign(models.Model):
    objects = TenantQuerySet.as_manager()

    CAMPAIGN_TYPES = [
        ("job_outreach", "Job Outreach"),
        ("geosentry_mining", "GeoSentry Mining"),
        ("freelance_clients", "Freelance Clients"),
        ("masters_scholarship", "Masters Scholarship"),
    ]

    name = models.CharField(max_length=200, unique=True)
    is_active = models.BooleanField(default=True)
    users = models.ManyToManyField(User, blank=True, related_name="campaigns")
    product_docs = models.TextField(blank=True)
    campaign_objective = models.TextField(blank=True)
    booking_link = models.URLField(max_length=500, blank=True)
    is_freemium = models.BooleanField(default=False)
    action_fraction = models.FloatField(default=0.2)
    seed_public_ids = models.JSONField(default=list, blank=True)
    model_blob = models.BinaryField(null=True, blank=True)

    campaign_type = models.CharField(max_length=100, default="job_outreach")
    target_audience_definition = models.TextField(blank=True)
    approved_offer = models.TextField(blank=True)
    target_data_source = models.TextField(blank=True)
    message_template_family = models.TextField(blank=True)
    follow_up_template_family = models.TextField(blank=True)
    minimum_delay = models.PositiveIntegerField(default=24)
    max_follow_ups = models.PositiveIntegerField(default=3)
    active_time_window = models.CharField(max_length=50, default="09:00-17:00")
    manual_approval_required = models.BooleanField(default=True)
    suppression_rules = models.JSONField(default=dict, blank=True)
    duplicate_prevention = models.JSONField(default=dict, blank=True)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="owned_campaigns")
    workspace = models.ForeignKey(
        "Workspace",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="campaigns",
    )
    campaign_purpose = models.ForeignKey(
        "CampaignPurpose",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="campaigns",
    )
    sender_identity = models.ForeignKey(
        "SenderIdentity",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="campaigns",
    )
    linkedin_profile = models.ForeignKey(
        "linkedin.LinkedInProfile",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="campaigns",
    )
    is_synthetic = models.BooleanField(default=False)
    synthetic_run = models.ForeignKey(
        "SyntheticDemonstrationRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="campaigns",
    )

    def clean(self):
        super().clean()
        if self.campaign_purpose:
            if not self.campaign_purpose.is_active:
                raise ValidationError("Cannot assign an inactive campaign purpose.")
            if not self.campaign_purpose.is_system and self.campaign_purpose.workspace != self.workspace:
                raise ValidationError("Campaign purpose workspace must match campaign workspace.")
            if self.campaign_type and self.campaign_type != self.campaign_purpose.slug:
                raise ValidationError("Campaign type string does not match campaign purpose slug.")
        if self.sender_identity and self.sender_identity.workspace != self.workspace:
            raise ValidationError("Campaign sender identity workspace must match campaign workspace.")

    def save(self, *args, **kwargs):
        self.full_clean()
        if self.campaign_purpose and not self.campaign_type:
            self.campaign_type = self.campaign_purpose.slug
        super().save(*args, **kwargs)

    # ── Campaign-level Caps (Batch 7) ──
    daily_connect_limit = models.PositiveIntegerField(
        null=True, blank=True, default=20, validators=[MinValueValidator(1)]
    )
    daily_follow_up_limit = models.PositiveIntegerField(
        null=True, blank=True, default=25, validators=[MinValueValidator(1)]
    )

    # ── Channel Controls ──
    linkedin_outreach_enabled = models.BooleanField(
        default=True,
        help_text="When enabled, qualified prospects are eligible for LinkedIn connection outreach.",
    )
    email_outreach_enabled = models.BooleanField(
        default=True,
        help_text="When enabled, qualified prospects are eligible for email resolution and outreach.",
    )

    # ── Connection note settings (Batch 2) ──
    connection_note_enabled = models.BooleanField(
        default=True,
        help_text="When enabled, every connection request includes a personalised note.",
    )
    connection_note_template = models.TextField(
        blank=True, default="",
        help_text=(
            "Optional custom template. Supported placeholders: "
            "{first_name}, {full_name}, {title}, {company}, {industry}, "
            "{location}, {campaign_name}, {company_or_industry}. "
            "Leave blank to use the built-in default."
        ),
    )
    connection_note_instructions = models.TextField(
        blank=True, default="",
        help_text=(
            "Optional extra instructions for the LLM note generator "
            "(e.g. 'mention our new product launch', 'keep it casual')."
        ),
    )

    # ── Detailed Targeting Parameters ──
    target_job_titles = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Target job titles (e.g. 'CEO, Founder, VP Marketing')."
    )
    target_seniority = models.CharField(
        max_length=256, blank=True, default="",
        help_text="Target seniority levels (e.g. 'Executive, C-Level, Director')."
    )
    target_industries = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Target industries (e.g. 'B2B SaaS, Technology, Financial Services')."
    )
    target_locations = models.CharField(
        max_length=256, blank=True, default="",
        help_text="Target geographic locations (e.g. 'United States, United Kingdom')."
    )
    target_locations_excluded = models.CharField(
        max_length=256, blank=True, default="",
        help_text="Excluded geographic locations (e.g. 'Germany, United States')."
    )
    unknown_location_handling = models.CharField(
        max_length=20, default="deny", blank=True,
        help_text="Behavior for unknown prospect location: 'deny', 'allow', 'review'."
    )
    target_company_size = models.CharField(
        max_length=256, blank=True, default="",
        help_text="Target company employee size (e.g. '11-50, 51-200')."
    )
    target_keywords = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Custom target keywords or search terms."
    )

    # ── Criterion Importance & Requirement Controls ──
    require_title_match = models.BooleanField(
        default=True,
        help_text="Require Title MATCH for STRONG qualification."
    )
    require_seniority_match = models.BooleanField(
        default=True,
        help_text="Require Seniority MATCH for STRONG qualification when configured."
    )
    require_location_match = models.BooleanField(
        default=True,
        help_text="Require Location MATCH for STRONG qualification when configured."
    )
    require_industry_match = models.BooleanField(
        default=False,
        help_text="Require Industry MATCH for STRONG qualification when configured."
    )
    require_company_size_match = models.BooleanField(
        default=False,
        help_text="Require Company Size MATCH for STRONG qualification when configured."
    )

    def get_location_targeting_policy(self):
        from openoutreach.core.services.location_targeting import LocationTargetingPolicy
        inc = [loc.strip() for loc in (self.target_locations or "").split(",") if loc.strip()]
        exc = [loc.strip() for loc in (self.target_locations_excluded or "").split(",") if loc.strip()]
        unknown_h = self.unknown_location_handling or "deny"
        
        # If no campaign-specific locations configured, fallback to workspace outreach defaults if available
        if not inc and not exc and self.workspace:
            auto_settings = getattr(self.workspace, "automation_settings", None)
            if auto_settings and (auto_settings.default_outreach_locations or auto_settings.default_outreach_locations_excluded):
                return auto_settings.get_location_targeting_policy()

        return LocationTargetingPolicy(
            included_locations=inc,
            excluded_locations=exc,
            unknown_location_handling=unknown_h,
            channel="campaign",
        )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(daily_connect_limit__isnull=True) | models.Q(daily_connect_limit__gte=1),
                name="campaign_daily_connect_limit_check"
            ),
            models.CheckConstraint(
                condition=models.Q(daily_follow_up_limit__isnull=True) | models.Q(daily_follow_up_limit__gte=1),
                name="campaign_daily_follow_up_limit_check"
            ),
        ]

    def __str__(self):
        return self.name


class TaskQuerySet(models.QuerySet):
    def pending(self):
        """Pending tasks, EMAIL first, then oldest-scheduled first.

        Email outranks the LinkedIn channels so a *ready* send always preempts
        a ready connect/follow_up/check_pending — on startup and on every claim.
        Email slots are always scheduled ``now``, so ranking them first never
        makes ``seconds_to_next`` oversleep a sooner LinkedIn task."""
        email_first = models.Case(
            models.When(task_type=Task.TaskType.EMAIL, then=models.Value(0)),
            default=models.Value(1),
            output_field=models.IntegerField(),
        )
        return self.filter(status=Task.Status.PENDING).order_by(email_first, "scheduled_at")

    def pending_executable(self):
        """Pending tasks for active (non-paused) campaigns or campaign-less internal tasks."""
        from django.db.models import Q
        paused_ids = list(Campaign.objects.filter(is_active=False).values_list("id", flat=True))
        qs = self.pending()
        if paused_ids:
            qs = qs.filter(
                Q(task_type__in=[
                    Task.TaskType.HEALTH_CHECK,
                    Task.TaskType.JOB_APPLICATION,
                    Task.TaskType.JOB_DISCOVERY,
                    Task.TaskType.PROSPECT_DISCOVERY,
                    Task.TaskType.REPLY_POLL,
                ])
                | Q(payload__campaign_id__isnull=True)
                | ~Q(payload__campaign_id__in=paused_ids)
            )
        return qs

    def claim_next(self) -> "Task | None":
        from django.db import transaction
        with transaction.atomic():
            task = self.select_for_update(skip_locked=True).pending_executable().filter(scheduled_at__lte=timezone.now()).first()
            if task:
                task.status = Task.Status.RUNNING
                task.started_at = timezone.now()
                task.save(update_fields=["status", "started_at"])
            return task

    def seconds_to_next(self) -> float | None:
        """Seconds until the next pending task, or None if queue is empty."""
        next_task = self.pending_executable().only("scheduled_at").first()
        if next_task is None:
            return None
        return max((next_task.scheduled_at - timezone.now()).total_seconds(), 0)


class Task(models.Model):
    class TaskType(models.TextChoices):
        CONNECT = "connect"
        CHECK_PENDING = "check_pending"
        FOLLOW_UP = "follow_up"
        EMAIL = "email"
        HEALTH_CHECK = "health_check"
        JOB_APPLICATION = "job_application"
        JOB_DISCOVERY = "job_discovery"
        PROSPECT_DISCOVERY = "prospect_discovery"
        REPLY_POLL = "reply_poll"

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        COMPLETED = "completed"
        FAILED = "failed"
        CANCELLED = "cancelled"

    task_type = models.CharField(max_length=25, choices=TaskType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    scheduled_at = models.DateTimeField()
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = TaskQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(
                fields=["status", "scheduled_at"],
                name="core_task_status_sched_idx",
            ),
        ]

    def __str__(self):
        return f"{self.task_type} [{self.status}] scheduled={self.scheduled_at}"

    def mark_running(self):
        self.status = self.Status.RUNNING
        self.started_at = self.started_at or timezone.now()
        self.save(update_fields=["status", "started_at"])

    def mark_completed(self):
        self.status = self.Status.COMPLETED
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at", "payload"])

    def mark_failed(self, reason: str | None = None, retriable: bool = False):
        self.status = self.Status.FAILED
        self.completed_at = timezone.now()
        if reason:
            self.payload["reason"] = reason
        self.payload["retriable"] = retriable
        self.save(update_fields=["status", "completed_at", "payload"])

    def mark_cancelled(self, reason: str | None = None):
        self.status = self.Status.CANCELLED
        self.completed_at = timezone.now()
        if reason:
            self.payload["cancellation_reason"] = reason
        self.save(update_fields=["status", "completed_at", "payload"])


class DaemonStatus(models.Model):
    """Stores visibility, heartbeat, and execution state of the HuntFlow daemon."""
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    is_running = models.BooleanField(default=False)
    pid = models.IntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    active_profile_username = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "Daemon Status"
        verbose_name_plural = "Daemon Statuses"

    def __str__(self):
        return f"Daemon Status (running={self.is_running}, heartbeat={self.last_heartbeat})"


class TaskQueueEvent(models.Model):
    task = models.ForeignKey("Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="queue_events")
    task_id_snapshot = models.IntegerField(null=True, blank=True)
    campaign = models.ForeignKey("Campaign", null=True, blank=True, on_delete=models.SET_NULL, related_name="queue_events")
    operator = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="queue_events")
    action = models.CharField(max_length=100)
    previous_status = models.CharField(max_length=20, blank=True)
    new_status = models.CharField(max_length=20, blank=True)
    previous_scheduled_at = models.DateTimeField(null=True, blank=True)
    new_scheduled_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    metadata = models.JSONField(null=True, blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Task Queue Event"
        verbose_name_plural = "Task Queue Events"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Queue Event: {self.action} on Task #{self.task_id_snapshot or '?'}"


class LaunchRun(models.Model):
    operator = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="launch_runs")
    source = models.CharField(max_length=50, default="unknown")  # dashboard, cli, system, unknown
    mode = models.CharField(max_length=20, default="live")  # live, preview
    status = models.CharField(max_length=50, default="unknown")  # blocked, started, running, stopped, failed, completed, unknown
    campaign = models.ForeignKey("Campaign", null=True, blank=True, on_delete=models.SET_NULL, related_name="launch_runs")
    daemon_pid = models.IntegerField(null=True, blank=True)
    readiness_status = models.CharField(max_length=20, blank=True)
    blocker_count = models.IntegerField(default=0)
    warning_count = models.IntegerField(default=0)
    readiness_snapshot = models.JSONField(null=True, blank=True, default=dict)
    queue_snapshot = models.JSONField(null=True, blank=True, default=dict)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    stop_reason = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(null=True, blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Launch Run"
        verbose_name_plural = "Launch Runs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Launch Run #{self.pk} (status={self.status}, mode={self.mode})"


class LaunchRunEvent(models.Model):
    launch_run = models.ForeignKey(LaunchRun, on_delete=models.CASCADE, related_name="events")
    operator = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="launch_run_events")
    event_type = models.CharField(max_length=100)
    note = models.TextField(blank=True)
    metadata = models.JSONField(null=True, blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Launch Run Event"
        verbose_name_plural = "Launch Run Events"
        ordering = ["created_at"]

    def __str__(self):
        return f"Event {self.event_type} on Run #{self.launch_run_id}"


class OperatorAlert(models.Model):
    class AlertStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        SNOOZED = "snoozed", "Snoozed"
        DISMISSED = "dismissed", "Dismissed"
        RESOLVED = "resolved", "Resolved"

    class SourceType(models.TextChoices):
        OPERATIONS = "operations", "Operations"
        READINESS = "readiness", "Readiness"
        DAEMON = "daemon", "Daemon"
        TASK_QUEUE = "task_queue", "Task Queue"
        LAUNCH = "launch", "Launch"
        CAMPAIGN = "campaign", "Campaign"
        SYSTEM = "system", "System"

    class SeverityType(models.TextChoices):
        HARD = "hard", "Hard"
        WARNING = "warning", "Warning"
        INFO = "info", "Info"

    workspace = models.ForeignKey("core.Workspace", null=True, blank=True, on_delete=models.CASCADE, related_name="operator_alerts")
    fingerprint = models.CharField(max_length=64, unique=True)
    source = models.CharField(max_length=50, choices=SourceType.choices, default=SourceType.SYSTEM)
    severity = models.CharField(max_length=20, choices=SeverityType.choices, default=SeverityType.INFO)
    status = models.CharField(max_length=20, choices=AlertStatus.choices, default=AlertStatus.ACTIVE)
    title = models.CharField(max_length=255)
    description = models.TextField()
    fix_link = models.CharField(max_length=255, blank=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    last_triggered_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="acknowledged_alerts")
    snoozed_until = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="dismissed_alerts")
    occurrence_count = models.IntegerField(default=1)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Operator Alert"
        verbose_name_plural = "Operator Alerts"
        ordering = ["-last_triggered_at"]

    def __str__(self):
        return f"[{self.severity.upper()}] {self.title} ({self.status})"


class OperatorAlertEvent(models.Model):
    class EventType(models.TextChoices):
        CREATED = "created", "Created"
        SEEN = "seen", "Seen"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        SNOOZED = "snoozed", "Snoozed"
        DISMISSED = "dismissed", "Dismissed"
        REOPENED = "reopened", "Reopened"
        RESOLVED = "resolved", "Resolved"
        NOTE_ADDED = "note_added", "Note Added"

    alert = models.ForeignKey(OperatorAlert, on_delete=models.CASCADE, related_name="events")
    operator = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="alert_events")
    event_type = models.CharField(max_length=50, choices=EventType.choices)
    note = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Operator Alert Event"
        verbose_name_plural = "Operator Alert Events"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.event_type} event on Alert #{self.alert_id}"


class Workspace(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    subscription_tier = models.CharField(
        max_length=50,
        default="free",
        choices=[
            ("free", "Free"),
            ("starter", "Starter"),
            ("professional", "Professional"),
            ("agency", "Agency"),
            ("custom", "Custom"),
        ]
    )
    status = models.CharField(max_length=50, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Workspace"
        verbose_name_plural = "Workspaces"

    def __str__(self):
        return self.name


class TenantQuerySet(models.QuerySet):
    def for_workspace(self, workspace):
        return self.filter(workspace=workspace)


class WorkspaceMembership(models.Model):
    ROLE_CHOICES = [
        ("owner", "Owner"),
        ("admin", "Admin"),
        ("campaign_manager", "Campaign Manager"),
        ("reviewer", "Reviewer"),
        ("operator", "Operator"),
        ("viewer", "Viewer"),
    ]

    STATUS_CHOICES = [
        ("active", "Active"),
        ("suspended", "Suspended"),
        ("invited", "Invited"),
        ("revoked", "Revoked"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="workspace_memberships")
    role = models.CharField(max_length=50, choices=ROLE_CHOICES, default="viewer")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Workspace Membership"
        verbose_name_plural = "Workspace Memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user"],
                name="unique_workspace_membership",
            )
        ]
        indexes = [
            models.Index(fields=["workspace", "user", "status"], name="ws_mem_ws_usr_stat_idx"),
        ]

    def clean(self):
        super().clean()
        if self.role not in dict(self.ROLE_CHOICES):
            raise ValidationError(f"Invalid role: {self.role}")
        if self.status not in dict(self.STATUS_CHOICES):
            raise ValidationError(f"Invalid status: {self.status}")
        if self.pk:
            old = WorkspaceMembership.objects.get(pk=self.pk)
            if old.role == "owner" and self.role != "owner":
                owners_count = WorkspaceMembership.objects.filter(workspace=self.workspace, role="owner", status="active").count()
                if owners_count <= 1:
                    raise ValidationError("The last owner cannot be demoted.")
            if old.role == "owner" and self.status == "suspended":
                owners_count = WorkspaceMembership.objects.filter(workspace=self.workspace, role="owner", status="active").count()
                if owners_count <= 1:
                    raise ValidationError("The last owner cannot be suspended.")

    def delete(self, *args, **kwargs):
        if self.role == "owner":
            owners_count = WorkspaceMembership.objects.filter(workspace=self.workspace, role="owner", status="active").count()
            if owners_count <= 1:
                raise ValidationError("The last owner cannot be removed.")
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} in {self.workspace.name} ({self.role}, {self.status})"


class CampaignPurpose(models.Model):
    workspace = models.ForeignKey(
        Workspace, null=True, blank=True, on_delete=models.CASCADE, related_name="campaign_purposes"
    )
    slug = models.SlugField(max_length=100)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    target_type = models.CharField(max_length=100, default="individual")
    objective = models.TextField(blank=True, default="")
    supported_channels = models.JSONField(default=list, blank=True)
    required_recipient_fields = models.JSONField(default=list, blank=True)
    required_sender_facts = models.JSONField(default=list, blank=True)
    required_personalization_facts = models.JSONField(default=list, blank=True)
    writing_policy = models.TextField(blank=True, default="")
    claim_validation_policy = models.TextField(blank=True, default="")
    max_connection_note_length = models.PositiveIntegerField(default=300)
    max_follow_ups = models.PositiveIntegerField(default=3)
    approval_policy = models.CharField(max_length=50, default="manual")
    sending_policy = models.CharField(max_length=50, default="draft_only")
    default_limits = models.JSONField(default=dict, blank=True)
    context_schema = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        verbose_name = "Campaign Purpose"
        verbose_name_plural = "Campaign Purposes"
        constraints = [
            models.UniqueConstraint(
                fields=["slug"],
                condition=models.Q(workspace__isnull=True),
                name="unique_system_purpose_slug",
            ),
            models.UniqueConstraint(
                fields=["workspace", "slug"],
                condition=models.Q(workspace__isnull=False),
                name="unique_tenant_purpose_slug",
            ),
        ]

    def clean(self):
        super().clean()
        if self.is_system and self.workspace is not None:
            raise ValidationError("System campaign purposes must have workspace set to None.")

    def delete(self, *args, **kwargs):
        if self.is_system:
            raise ValidationError("System campaign purposes cannot be deleted.")
        super().delete(*args, **kwargs)

    def __str__(self):
        prefix = "[System]" if self.is_system else f"[{self.workspace.name}]"
        return f"{prefix} {self.name} ({self.slug})"


class PromptPack(models.Model):
    campaign_purpose = models.ForeignKey(CampaignPurpose, on_delete=models.CASCADE, related_name="prompt_packs")
    name = models.CharField(max_length=200)
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PromptPack: {self.name} v{self.version}"


class PromptPackVersion(models.Model):
    prompt_pack = models.ForeignKey(PromptPack, on_delete=models.CASCADE, related_name="versions")
    version_number = models.PositiveIntegerField()
    prompt_template = models.TextField()
    context_schema = models.JSONField(default=dict, blank=True)
    writing_policy = models.TextField(blank=True, default="")
    claims_policy = models.TextField(blank=True, default="")
    checksum = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prompt_pack", "version_number"],
                name="unique_prompt_pack_version",
            )
        ]

    def __str__(self):
        return f"{self.prompt_pack.name} (v{self.version_number})"


class CampaignPurposeVersion(models.Model):
    campaign_purpose = models.ForeignKey(CampaignPurpose, on_delete=models.CASCADE, related_name="purpose_versions")
    version_number = models.PositiveIntegerField()
    writing_policy_version = models.CharField(max_length=50, default="v1")
    claims_policy_version = models.CharField(max_length=50, default="v1")
    context_schema = models.JSONField(default=dict, blank=True)
    checksum = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["campaign_purpose", "version_number"],
                name="unique_campaign_purpose_version",
            )
        ]

    def __str__(self):
        return f"{self.campaign_purpose.name} Version {self.version_number}"


class MessageSequenceStep(models.Model):
    CHANNEL_CHOICES = [
        ("linkedin", "LinkedIn"),
        ("email", "Email"),
        ("manual", "Manual Task"),
    ]
    STEP_TYPE_CHOICES = [
        ("connection_note", "Connection Note"),
        ("first_message", "First Message"),
        ("follow_up", "Follow Up"),
        ("second_follow_up", "Second Follow Up"),
        ("email_outbound", "Email Outbound"),
    ]

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="sequence_steps")
    sequence_number = models.PositiveIntegerField()
    channel = models.CharField(max_length=50, choices=CHANNEL_CHOICES, default="linkedin")
    step_type = models.CharField(max_length=50, choices=STEP_TYPE_CHOICES, default="connection_note")
    delay_hours = models.PositiveIntegerField(default=24)
    prompt_version = models.ForeignKey(PromptPackVersion, null=True, blank=True, on_delete=models.SET_NULL)
    prompt_template = models.TextField(blank=True, default="")
    length_limit = models.PositiveIntegerField(default=300)
    required_context_schema = models.JSONField(default=dict, blank=True)
    approval_requirement = models.BooleanField(default=True)
    stop_on_reply = models.BooleanField(default=True)
    regeneration_policy = models.CharField(max_length=50, default="standard")
    is_enabled = models.BooleanField(default=True)
    sending_policy = models.CharField(max_length=50, default="draft_only")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Message Sequence Step"
        verbose_name_plural = "Message Sequence Steps"
        ordering = ["campaign", "sequence_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "sequence_number"],
                name="unique_campaign_sequence_number",
            )
        ]

    def clean(self):
        super().clean()
        if self.prompt_version and self.campaign.workspace:
            p_pack_purpose = self.prompt_version.prompt_pack.campaign_purpose
            if not p_pack_purpose.is_system and p_pack_purpose.workspace != self.campaign.workspace:
                raise ValidationError("Sequence step prompt pack must belong to the same workspace or be a system purpose.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Step #{self.sequence_number} ({self.channel}:{self.step_type}) for {self.campaign.name}"


class SenderIdentity(models.Model):
    SENDER_TYPE_CHOICES = [
        ("individual", "Individual"),
        ("company_brand", "Company Brand"),
        ("executive_proxy", "Executive Proxy"),
        ("academic_candidacy", "Academic Candidacy"),
    ]
    VERIFICATION_STATUS_CHOICES = [
        ("pending", "Pending"),
        ("verified", "Verified"),
        ("rejected", "Rejected"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="sender_identities")
    display_name = models.CharField(max_length=200)
    sender_type = models.CharField(max_length=50, choices=SENDER_TYPE_CHOICES, default="individual")
    email = models.EmailField()
    linkedin_profile_url = models.URLField(max_length=500, blank=True, default="")
    company_or_brand = models.CharField(max_length=200, blank=True, default="")
    signature = models.TextField(blank=True, default="")
    verification_status = models.CharField(max_length=20, choices=VERIFICATION_STATUS_CHOICES, default="pending")
    is_active = models.BooleanField(default=True)
    allowed_purposes = models.ManyToManyField(CampaignPurpose, through="SenderIdentityAllowedPurpose", blank=True)
    verified_facts = models.JSONField(default=list, blank=True)
    provenance_checksum = models.CharField(max_length=64, blank=True, default="")
    is_synthetic = models.BooleanField(default=False)
    synthetic_run = models.ForeignKey(
        "SyntheticDemonstrationRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sender_identities",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        verbose_name = "Sender Identity"
        verbose_name_plural = "Sender Identities"

    @property
    def external_action_authorized(self) -> bool:
        return self.verification_status == "verified" and self.is_active

    def clean(self):
        super().clean()
        if self.verification_status != "verified" and getattr(self, "_attempting_external_action", False):
            raise ValidationError("Unverified sender identity cannot authorize external sending.")

    def __str__(self):
        return f"{self.display_name} <{self.email}> [{self.verification_status}]"


class SenderIdentityAllowedPurpose(models.Model):
    sender_identity = models.ForeignKey(SenderIdentity, on_delete=models.CASCADE)
    campaign_purpose = models.ForeignKey(CampaignPurpose, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["sender_identity", "campaign_purpose"],
                name="unique_sender_allowed_purpose",
            )
        ]


class CampaignPurposeSequenceTemplate(models.Model):
    campaign_purpose = models.ForeignKey(CampaignPurpose, on_delete=models.CASCADE, related_name="sequence_templates")
    sequence_number = models.PositiveIntegerField()
    channel = models.CharField(max_length=50, default="linkedin")
    step_type = models.CharField(max_length=50, default="connection_note")
    delay_hours = models.PositiveIntegerField(default=24)
    length_limit = models.PositiveIntegerField(default=300)
    approval_requirement = models.BooleanField(default=True)
    sending_policy = models.CharField(max_length=50, default="draft_only")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Campaign Purpose Sequence Template"
        ordering = ["campaign_purpose", "sequence_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign_purpose", "sequence_number"],
                name="unique_purpose_template_sequence_number",
            )
        ]

    def __str__(self):
        return f"{self.campaign_purpose.slug} Step #{self.sequence_number} ({self.channel}:{self.step_type})"


class CampaignMessageDraft(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("reviewed", "Reviewed"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="message_drafts")
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="message_drafts")
    deal = models.ForeignKey("crm.Deal", on_delete=models.CASCADE, related_name="message_drafts")
    lead = models.ForeignKey("crm.Lead", on_delete=models.CASCADE, related_name="message_drafts")
    sender_identity = models.ForeignKey(SenderIdentity, null=True, blank=True, on_delete=models.SET_NULL, related_name="message_drafts")
    sequence_step = models.ForeignKey(MessageSequenceStep, on_delete=models.CASCADE, related_name="message_drafts")
    purpose_version = models.ForeignKey(CampaignPurposeVersion, null=True, blank=True, on_delete=models.SET_NULL)
    prompt_version = models.ForeignKey(PromptPackVersion, null=True, blank=True, on_delete=models.SET_NULL)
    content = models.TextField()
    content_checksum = models.CharField(max_length=64, blank=True, default="")
    style_validation_result = models.JSONField(default=dict, blank=True)
    claims_validation_result = models.JSONField(default=dict, blank=True)
    context_snapshot = models.JSONField(default=dict, blank=True)
    sender_fact_snapshot = models.JSONField(default=list, blank=True)
    generation_model_metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    reviewer = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default="")
    draft_mode = models.BooleanField(default=True)
    sendable = models.BooleanField(default=False)
    external_action_authorized = models.BooleanField(default=False)
    is_synthetic = models.BooleanField(default=False)
    synthetic_run = models.ForeignKey(
        "SyntheticDemonstrationRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="message_drafts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        verbose_name = "Campaign Message Draft"
        verbose_name_plural = "Campaign Message Drafts"
        ordering = ["-created_at"]

    def clean(self):
        super().clean()
        self.draft_mode = True
        self.sendable = False
        self.external_action_authorized = False

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Draft for Deal #{self.deal_id} Step #{self.sequence_step_id} [{self.status}]"


class SyntheticDemonstrationRun(models.Model):
    run_key = models.CharField(max_length=100, unique=True)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="synthetic_runs")
    status = models.CharField(max_length=20, default="completed")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Synthetic Run {self.run_key} ({self.status})"


class AuditEvent(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="audit_events")
    event_type = models.CharField(max_length=100)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    target_type = models.CharField(max_length=100, blank=True, default="")
    target_id = models.CharField(max_length=100, blank=True, default="")
    result = models.CharField(max_length=20, default="success")
    action_key = models.CharField(max_length=100, blank=True, null=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        verbose_name = "Audit Event"
        verbose_name_plural = "Audit Events"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "event_type", "action_key"],
                name="unique_workspace_event_action_key",
                condition=models.Q(action_key__isnull=False) & ~models.Q(action_key=""),
            )
        ]

    def __str__(self):
        return f"[{self.workspace.slug}] {self.event_type} ({self.result}) at {self.created_at}"


# ── Phase 5: Commercial Foundation & Operations Models ─────────────────

class SubscriptionPlan(models.Model):
    slug = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    monthly_price_usd = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    max_campaigns = models.IntegerField(default=-1, help_text="-1 means unlimited")
    max_monthly_leads = models.IntegerField(default=1000)
    max_sender_identities = models.IntegerField(default=5)
    max_members = models.IntegerField(default=3)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Subscription Plan"
        verbose_name_plural = "Subscription Plans"

    def __str__(self):
        return f"{self.name} (${self.monthly_price_usd}/mo)"


class WorkspaceEntitlement(models.Model):
    STATUS_CHOICES = [
        ("active", "Active"),
        ("trialing", "Trialing"),
        ("past_due", "Past Due"),
        ("canceled", "Canceled"),
    ]

    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE, related_name="entitlement")
    plan = models.ForeignKey(SubscriptionPlan, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="trialing")
    current_period_start = models.DateTimeField(default=timezone.now)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Workspace Entitlement"
        verbose_name_plural = "Workspace Entitlements"

    def __str__(self):
        plan_name = self.plan.name if self.plan else "No Plan"
        return f"[{self.workspace.slug}] {plan_name} ({self.status})"


class WorkspaceAutomationSettings(models.Model):
    """
    Workspace-scoped automation configuration.
    Ensures master automation and sub-switches are strictly tenant-isolated.
    """
    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE, related_name="automation_settings")
    master_automation_enabled = models.BooleanField(default=False)
    auto_find_prospects = models.BooleanField(default=False)
    auto_send_connections = models.BooleanField(default=False)
    auto_followup = models.BooleanField(default=False)
    auto_find_jobs = models.BooleanField(default=False)
    auto_apply_jobs = models.BooleanField(default=False)
    auto_send_emails = models.BooleanField(default=False)
    auto_monitor_replies = models.BooleanField(default=False)

    MODE_CHOICES = [
        ("PAUSED", "Paused"),
        ("COMMISSIONING", "Commissioning"),
        ("BOUNDED_PRODUCTION", "Bounded Production"),
        ("BOUNDED_MULTI_CHANNEL_PRODUCTION", "Bounded Multi-Channel Production"),
        ("FULL_AUTONOMOUS", "Full Autonomous"),
    ]
    BACKLOG_BEHAVIOR_CHOICES = [
        ("PAUSE_NEW_CONNECTIONS", "Pause New Connections"),
        ("WARN_ONLY", "Warn Only"),
        ("ALLOW_WITHIN_OPERATOR_LIMIT", "Allow Within Limit"),
    ]

    mode = models.CharField(max_length=40, choices=MODE_CHOICES, default="COMMISSIONING")

    # ── Durable Daily Limits & Objectives ──
    max_daily_job_applications = models.IntegerField(default=2, help_text="Maximum allowed job applications per calendar day.")
    daily_job_application_target = models.IntegerField(default=50, help_text="Daily target objective for job applications.")
    easy_apply_priority = models.BooleanField(default=True, help_text="Prioritize LinkedIn Easy Apply applications.")
    jobs_inspected_last_cycle = models.IntegerField(default=0, help_text="Count of jobs inspected during the most recent discovery cycle.")
    safe_jobs_last_cycle = models.IntegerField(default=0, help_text="Count of safe, eligible jobs found during the most recent discovery cycle.")
    job_discovery_query_index = models.PositiveIntegerField(default=0, help_text="Current rotation index for LinkedIn job queries.")
    last_job_discovery_started_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when job discovery was last started.")
    last_job_discovery_completed_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when job discovery was last completed.")
    last_job_application_attempt_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when the most recent job application was attempted.")
    last_job_scheduler_error = models.CharField(max_length=255, blank=True, default="", help_text="Most recent error message encountered during job discovery/scheduling.")
    max_daily_connections = models.IntegerField(default=5, help_text="Maximum allowed connection requests per calendar day.")
    max_daily_messages = models.IntegerField(default=10, help_text="Maximum allowed messages / follow-ups per calendar day.")
    max_daily_ai_replies = models.IntegerField(default=10, help_text="Maximum allowed autonomous AI replies per calendar day.")
    max_daily_emails = models.IntegerField(default=0, help_text="Maximum allowed cold emails per calendar day.")

    # ── Pending Invitation Backlog Risk Controls ──
    max_pending_invitations = models.IntegerField(default=100, help_text="Maximum outstanding sent connection invitations before pausing new invites.")
    stale_invitation_age_days = models.IntegerField(default=30, help_text="Age threshold in days to classify sent invitations as stale.")
    connection_backlog_behavior = models.CharField(max_length=40, choices=BACKLOG_BEHAVIOR_CHOICES, default="PAUSE_NEW_CONNECTIONS")
    backlog_pause_reason = models.CharField(max_length=255, blank=True, default="")

    # ── Real LinkedIn Provider Backlog Cache ──
    linkedin_pending_invitations_count = models.IntegerField(default=0, help_text="Most recent count of sent outstanding invitations from LinkedIn provider.")
    linkedin_pending_invitations_observed_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the most recent LinkedIn provider invitation audit.")
    linkedin_pending_invitations_source = models.CharField(max_length=50, blank=True, default="provider_aggregate")

    # ── Explicit Operator Backlog Override ──
    connection_backlog_operator_override = models.BooleanField(
        default=False,
        help_text="Operator override permitting new connection requests despite invitation backlog exceeding limit."
    )
    backlog_override_source = models.CharField(max_length=50, blank=True, default="")
    backlog_override_timestamp = models.DateTimeField(null=True, blank=True)
    backlog_override_reason = models.CharField(max_length=255, blank=True, default="")

    # ── Daemon Heartbeat ──
    last_daemon_heartbeat = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the most recent rundaemon heartbeat.")

    # ── Real-Time Channel Execution Heartbeats ──
    last_prospect_discovery_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the most recent prospect discovery cycle.")
    last_connection_planner_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when connect planner last ran.")
    last_connection_attempt_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the most recent connection attempt.")
    last_acceptance_poll_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the most recent check_pending poll.")
    last_reply_poll_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the most recent inbound reply poll.")
    last_followup_planner_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when follow-up planner last ran.")
    last_ai_reply_evaluation_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the most recent AI reply evaluation.")
    last_email_cycle_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when email queue was last flushed.")

    # ── 24-Hour Bounded Soak Controls ──
    soak_started_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when the bounded unattended soak commenced.")
    soak_expected_end_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when the bounded soak automatically expires.")
    soak_duration_hours = models.IntegerField(default=24, help_text="Configured duration of the bounded soak in hours.")
    soak_status = models.CharField(
        max_length=30,
        default="NOT_STARTED",
        choices=[
            ("NOT_STARTED", "Not Started"),
            ("RUNNING", "Running"),
            ("EXPIRED", "Expired"),
            ("PAUSED", "Paused"),
            ("SAFETY_STOP", "Safety Stop"),
        ]
    )
    soak_stop_reason = models.CharField(max_length=255, blank=True, default="")
    provider_safety_state = models.CharField(
        max_length=60,
        default="NORMAL",
        choices=[
            ("NORMAL", "Normal"),
            ("HIGH_VOLUME_DATA_ACCESS_WARNING", "High Volume Data Access Warning"),
            ("ACCOUNT_RESTRICTED", "Account Restricted"),
            ("CHECKPOINT_REQUIRED", "Checkpoint Required"),
        ],
        help_text="Provider safety state. HIGH_VOLUME_DATA_ACCESS_WARNING requires explicit operator clearance."
    )
    manual_provider_use_paused = models.BooleanField(
        default=False,
        help_text="Operator manual LinkedIn usage pause. Blocks provider browser operations while preserving all 8 automation preferences.",
    )
    manual_provider_use_paused_at = models.DateTimeField(null=True, blank=True)
    manual_provider_use_resumed_at = models.DateTimeField(null=True, blank=True)
    connection_inventory_low_water = models.PositiveIntegerField(
        default=5,
        help_text="Replenish READY_TO_CONNECT deals when inventory drops below this threshold."
    )
    connection_inventory_high_water = models.PositiveIntegerField(
        default=20,
        help_text="Halt live prospect searches when READY_TO_CONNECT deals reach or exceed this threshold."
    )
    job_query_performance_scores = models.JSONField(
        default=dict,
        blank=True,
        help_text="Historical query performance stats (match rate, easy apply rate, safe jobs found)."
    )

    # ── Global Default Outreach Geography ──
    default_outreach_locations = models.CharField(
        max_length=256, blank=True, default="",
        help_text="Default geographic targets for outreach (e.g. 'United States, United Kingdom')."
    )
    default_outreach_locations_excluded = models.CharField(
        max_length=256, blank=True, default="",
        help_text="Default excluded geographic targets for outreach."
    )
    default_unknown_location_handling = models.CharField(
        max_length=20, default="deny", blank=True,
        help_text="Default handling for unknown prospect location: 'deny', 'allow', 'review'."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Workspace Automation Settings"
        verbose_name_plural = "Workspace Automation Settings"

    def __str__(self):
        status = "RUNNING" if self.master_automation_enabled else "PAUSED"
        return f"[{self.workspace.slug}] Automation: {status} ({self.mode})"

    def start_bounded_multi_channel_production(
        self,
        max_daily_jobs: int = 50,
        max_daily_conns: int = 40,
        max_daily_msgs: int = 90,
        max_daily_ai_replies: int = 90,
        max_daily_emails: int = 25,
        backlog_override: bool = True,
    ):
        """Activates full multi-channel bounded production across all configured channels."""
        from django.utils import timezone
        self.mode = "BOUNDED_MULTI_CHANNEL_PRODUCTION"
        self.master_automation_enabled = True
        self.auto_apply_jobs = True
        self.auto_send_connections = True
        self.auto_followup = True
        self.auto_monitor_replies = True
        self.max_daily_job_applications = max_daily_jobs
        self.max_daily_connections = max_daily_conns
        self.max_daily_messages = max_daily_msgs
        self.max_daily_ai_replies = max_daily_ai_replies
        self.max_daily_emails = max_daily_emails
        self.soak_status = "RUNNING"
        self.soak_stop_reason = ""

        # Check if email is genuinely configured with a mailbox and email-enabled campaign
        from openoutreach.emails.models import Mailbox
        from openoutreach.core.models import Campaign
        has_email_box = Mailbox.objects.filter(workspace=self.workspace).exists()
        has_email_camp = Campaign.objects.filter(workspace=self.workspace, is_active=True, email_outreach_enabled=True).exists()
        self.auto_send_emails = bool(has_email_box and has_email_camp)

        if backlog_override:
            self.connection_backlog_operator_override = True
            self.backlog_override_source = "operator"
            self.backlog_override_timestamp = timezone.now()
            self.backlog_override_reason = "operator_explicitly_requested_all_channels_running"

        self.save()

    def update_linkedin_pending_invitations(self, count: int, source: str = "provider_aggregate"):
        """Durably caches the verified LinkedIn provider pending sent invitation count."""
        from django.utils import timezone
        self.linkedin_pending_invitations_count = count
        self.linkedin_pending_invitations_observed_at = timezone.now()
        self.linkedin_pending_invitations_source = source
        self.save(update_fields=[
            "linkedin_pending_invitations_count",
            "linkedin_pending_invitations_observed_at",
            "linkedin_pending_invitations_source",
            "updated_at",
        ])

    def enable_backlog_override(self, source: str = "operator", reason: str = "operator_explicitly_requested_all_channels_running"):
        from django.utils import timezone
        self.connection_backlog_operator_override = True
        self.backlog_override_source = source
        self.backlog_override_timestamp = timezone.now()
        self.backlog_override_reason = reason
        self.save(update_fields=["connection_backlog_operator_override", "backlog_override_source", "backlog_override_timestamp", "backlog_override_reason", "updated_at"])

    def disable_backlog_override(self):
        self.connection_backlog_operator_override = False
        self.save(update_fields=["connection_backlog_operator_override", "updated_at"])

    def pause_for_manual_provider_use(self) -> bool:
        """Pauses provider execution for operator manual LinkedIn use.
        
        Guarantees:
        - Blocks provider reads involving browser/network
        - Blocks provider writes
        - Blocks provider browser launch
        - Blocks provider task planning in scheduler
        - Blocks worker provider queue reconciliation
        - Unconditionally preserves all 8 user automation preferences
        - Preserves CRM, local AI, and unrelated SaaS features
        - Does NOT alter soak_status or provider_safety_state (distinguishes user pause from safety failures)
        """
        from django.utils import timezone
        self.manual_provider_use_paused = True
        self.manual_provider_use_paused_at = timezone.now()
        self.save(update_fields=["manual_provider_use_paused", "manual_provider_use_paused_at", "updated_at"])
        logger.info("Manual provider use pause activated for workspace %s", getattr(self.workspace, "slug", self.workspace_id))
        return True

    def resume_from_manual_provider_use(self) -> Tuple[bool, str]:
        """Resumes provider execution from manual provider use pause.
        
        Safety gating:
        - Only succeeds if provider_safety_state is NORMAL
        - Only succeeds if soak_status is not SAFETY_STOP
        - Does NOT clear any underlying safety holds or checkpoints
        """
        from django.utils import timezone
        if self.provider_safety_state != "NORMAL":
            return False, f"cannot_resume_provider_safety_state_{self.provider_safety_state.lower()}"
        if self.soak_status == "SAFETY_STOP":
            return False, f"cannot_resume_safety_stop_active_{self.soak_stop_reason}"

        self.manual_provider_use_paused = False
        self.manual_provider_use_resumed_at = timezone.now()
        self.save(update_fields=["manual_provider_use_paused", "manual_provider_use_resumed_at", "updated_at"])
        logger.info("Manual provider use pause cleared for workspace %s", getattr(self.workspace, "slug", self.workspace_id))
        return True, "ok"

    def start_bounded_soak(self, duration_hours: int = 24, max_daily_jobs: int = 5):
        from django.utils import timezone
        import datetime
        now = timezone.now()
        self.mode = "COMMISSIONING"
        self.master_automation_enabled = True
        self.auto_apply_jobs = True
        self.auto_send_connections = False
        self.auto_followup = False
        self.auto_send_emails = False
        self.auto_monitor_replies = True
        self.max_daily_job_applications = max_daily_jobs
        self.soak_started_at = now
        self.soak_duration_hours = duration_hours
        self.soak_expected_end_at = now + datetime.timedelta(hours=duration_hours)
        self.soak_status = "RUNNING"
        self.soak_stop_reason = ""
        self.save()

    def enable_persistent_production(
        self,
        enable_connections: bool = True,
        enable_followup: bool = True,
        enable_jobs: bool = True,
        enable_emails: bool = False,
        job_target: int = 50,
        job_ceiling: int = 60,
        connection_target: int = 40,
        message_target: int = 90,
        operator_cleared_warning: bool = False,
    ):
        """Activates continuous, persistent bounded production that does not expire after 24 hours."""
        if self.provider_safety_state == "HIGH_VOLUME_DATA_ACCESS_WARNING" and not operator_cleared_warning:
            logger.warning("Cannot auto-enable production while provider safety warning is active without operator clearance.")
            return False

        if operator_cleared_warning:
            self.provider_safety_state = "NORMAL"

        self.mode = "BOUNDED_PRODUCTION"
        self.master_automation_enabled = True
        self.auto_apply_jobs = enable_jobs
        self.auto_find_jobs = enable_jobs
        self.auto_send_connections = enable_connections
        self.auto_followup = enable_followup
        self.auto_send_emails = enable_emails
        self.auto_monitor_replies = True
        self.daily_job_application_target = job_target
        self.max_daily_job_applications = job_ceiling
        self.max_daily_connections = connection_target
        self.max_daily_messages = message_target
        self.soak_status = "RUNNING"
        self.soak_stop_reason = ""
        self.save()
        return True

    def check_soak_expiry(self) -> bool:
        """Evaluates whether a commissioning soak has reached expiration. Persistent production modes do not expire."""
        from django.utils import timezone
        if self.mode == "COMMISSIONING" and self.soak_status == "RUNNING" and self.soak_expected_end_at:
            if timezone.now() >= self.soak_expected_end_at:
                self.soak_status = "EXPIRED"
                self.master_automation_enabled = False
                self.auto_apply_jobs = False
                self.auto_send_connections = False
                self.auto_followup = False
                self.auto_send_emails = False
                self.soak_stop_reason = "soak_duration_elapsed_24h"
                self.save()
                return True
        return False

    RECOVERABLE_SESSION_STOP_REASONS = {
        "provider_captcha",
        "provider_checkpoint",
        "provider_challenge",
        "provider_auth_redirect_loop",
    }

    def reconcile_safety_invariants(self) -> bool:
        """Enforces consistency between soak_status, soak_stop_reason, and provider_safety_state.
        Specifically, prevents the inconsistent state where provider_safety_state is NORMAL
        yet soak_status remains SAFETY_STOP from a recoverable session reason."""
        changed = False
        norm_reason = (self.soak_stop_reason or "").strip().lower()
        if self.provider_safety_state == "NORMAL" and self.soak_status == "SAFETY_STOP" and norm_reason in self.RECOVERABLE_SESSION_STOP_REASONS:
            self.soak_status = "ACTIVE"
            self.soak_stop_reason = ""
            changed = True
        elif self.provider_safety_state == "CHECKPOINT_REQUIRED" and self.soak_status != "SAFETY_STOP":
            self.soak_status = "SAFETY_STOP"
            if not self.soak_stop_reason:
                self.soak_stop_reason = "provider_checkpoint"
            changed = True
        return changed

    def recover_from_session_health(self, classification: str, profile=None) -> bool:
        """Canonically updates workspace automation safety state following a session health check.

        Guarantees:
        - Recovers recoverable session safety stop (provider_captcha, provider_checkpoint, provider_challenge, provider_auth_redirect_loop)
          to soak_status='ACTIVE', soak_stop_reason='', provider_safety_state='NORMAL' when HEALTHY.
        - Strictly preserves unrelated safety stops (ACCOUNT_RESTRICTED, HIGH_VOLUME_DATA_ACCESS_WARNING,
          operator incident holds, quota limits, manual safety stops, target-specific redirect loops) even if a session is HEALTHY.
        - Preserves all 8 user-configured automation preference switches unconditionally.
        - Enforces internal state invariants consistently without requiring manual DB repairs.
        """
        canonical = (classification or "").upper().strip()
        if canonical in ["VALID"]:
            canonical = "HEALTHY"
        elif canonical in ["EXPIRED"]:
            canonical = "AUTH_REQUIRED"
        elif canonical in ["RESTRICTED"]:
            canonical = "RESTRICTION"

        changed = False
        norm_reason = (self.soak_stop_reason or "").strip().lower()

        if canonical == "HEALTHY":
            # 1. Recover checkpoint safety state if present
            if self.provider_safety_state == "CHECKPOINT_REQUIRED":
                self.provider_safety_state = "NORMAL"
                changed = True

            # 2. Recover soak status if stopped specifically due to recoverable session auth condition
            if self.soak_status == "SAFETY_STOP" and norm_reason in self.RECOVERABLE_SESSION_STOP_REASONS:
                self.soak_status = "ACTIVE"
                self.soak_stop_reason = ""
                changed = True

            # 3. Enforce invariant consistency
            if self.reconcile_safety_invariants():
                changed = True

            if changed:
                self.save(update_fields=["provider_safety_state", "soak_status", "soak_stop_reason", "updated_at"])
            return changed

        elif canonical in ["CHECKPOINT", "CHALLENGE", "CAPTCHA"]:
            reason = f"provider_{canonical.lower()}"
            self.trigger_safety_stop(reason)
            return True

        elif canonical == "RESTRICTION":
            self.trigger_safety_stop("provider_restriction")
            return True

        elif canonical == "HIGH_VOLUME_WARNING":
            self.trigger_safety_stop("high_volume_data_access_warning")
            return True

        elif canonical in ["REDIRECT_LOOP", "AUTH_REDIRECT_LOOP"]:
            self.trigger_safety_stop("provider_auth_redirect_loop")
            return True

        return False

    def reconcile_target_scoped_safety_hold(
        self,
        deal: Optional[Any] = None,
        lead: Optional[Any] = None,
        approval: Optional[Any] = None,
    ) -> Tuple[bool, str]:
        """Canonically reconciles a target-scoped redirect loop safety hold.

        Distinguishes target-scoped redirect loops (e.g. Lead profile navigation failing with
        ERR_TOO_MANY_REDIRECTS) from account-level authentication redirect loops.

        Invariants enforced:
        1. Workspace must be currently in soak_status == 'SAFETY_STOP' with an ambiguous or target
           redirect loop stop reason ('provider_redirect_loop' or 'provider_target_redirect_loop').
        2. Account session itself must be independently HEALTHY:
           Active LinkedInProfile has session_status == 'valid'.
           provider_safety_state is NORMAL (no unresolved CAPTCHA, checkpoint, restriction, or high volume).
        3. Write transport boundary was NOT crossed for the affected target (no confirmed write intents
           or confirmed connection attempts).
        4. Target-scoped failure is durably recorded:
           - Target Deal is marked FAILED (DealState.FAILED, reason recorded).
           - Target Lead is suppressed in SuppressionRecord to prevent duplicate reattempts.
           - Associated OperatorActionApproval is revoked and made permanently ineligible for execution.
        5. Clears the workspace soak_status to ACTIVE and soak_stop_reason to '' while preserving
           all 8 user-configured automation preference switches unconditionally.
        """
        norm_reason = (self.soak_stop_reason or "").strip().lower()
        if self.soak_status != "SAFETY_STOP" or norm_reason not in [
            "provider_redirect_loop",
            "provider_target_redirect_loop",
        ]:
            return False, f"workspace_not_in_redirect_safety_stop (current_status={self.soak_status}, reason={self.soak_stop_reason})"

        # 1. Verify account provider safety state is NORMAL
        if self.provider_safety_state != "NORMAL":
            return False, f"account_safety_state_not_normal ({self.provider_safety_state})"

        # 2. Verify active profile session status is valid/healthy
        from openoutreach.linkedin.models import (
            LinkedInProfile,
            ProviderWriteIntent,
            ConnectionAttempt,
        )
        from openoutreach.crm.models import Deal, DealState, SuppressionRecord, Lead
        from openoutreach.core.models import OperatorActionApproval

        active_profile = LinkedInProfile.objects.filter(
            workspace=self.workspace,
            active=True,
        ).first()
        if not active_profile or active_profile.session_status != "valid":
            prof_status = active_profile.session_status if active_profile else "missing"
            return False, f"account_session_not_valid ({prof_status})"

        # 3. Resolve target Deal and Lead if not explicitly provided
        target_deal = deal
        target_lead = lead
        target_approval = approval

        if not target_deal:
            if target_approval and getattr(target_approval, "deal", None):
                target_deal = target_approval.deal
            else:
                found_approval = OperatorActionApproval.objects.filter(
                    workspace=self.workspace,
                    status__in=["approved", "claimed"],
                    deal__isnull=False,
                ).order_by("-id").first()
                if found_approval:
                    target_approval = found_approval
                    target_deal = found_approval.deal

        if target_deal and not target_lead:
            target_lead = getattr(target_deal, "lead", None)

        # 4. Verify write transport boundary was NOT crossed
        target_identities = []
        if target_lead:
            if getattr(target_lead, "public_identifier", None):
                target_identities.append(target_lead.public_identifier)
            if getattr(target_lead, "linkedin_url", None):
                target_identities.append(target_lead.linkedin_url)

        if target_identities:
            confirmed_writes = ProviderWriteIntent.objects.filter(
                workspace=self.workspace,
                target_identity__in=target_identities,
                state__in=["confirmed", "uncertain", "provider_attempted"],
            )
            if confirmed_writes.exists():
                return False, "write_boundary_already_crossed_in_provider_write_intent"

            confirmed_attempts = ConnectionAttempt.objects.filter(
                recipient_public_id__in=target_identities,
                status="externally_confirmed",
            )
            if confirmed_attempts.exists():
                return False, "write_boundary_already_crossed_in_connection_attempt"

        # 5. Fail closed on the target
        if target_deal:
            target_deal.state = DealState.FAILED
            target_deal.reason = "provider_target_redirect_loop: net::ERR_TOO_MANY_REDIRECTS"
            target_deal.save(update_fields=["state", "reason"])

        if target_lead and getattr(target_lead, "linkedin_url", None):
            SuppressionRecord.objects.get_or_create(
                workspace=self.workspace,
                linkedin_url=target_lead.linkedin_url,
                defaults={"reason": "provider_target_redirect_loop: net::ERR_TOO_MANY_REDIRECTS"},
            )

        if target_approval:
            target_approval.status = "revoked"
            target_approval.claimed_at = None
            target_approval.failure_reason = "Target redirect loop: target profile unavailable"
            target_approval.save(update_fields=["status", "claimed_at", "failure_reason"])
        elif target_deal:
            for app in OperatorActionApproval.objects.filter(deal=target_deal, status="approved"):
                app.status = "revoked"
                app.claimed_at = None
                app.failure_reason = "Target redirect loop: target profile unavailable"
                app.save(update_fields=["status", "claimed_at", "failure_reason"])

        # 6. Reconcile workspace safety hold
        self.soak_status = "ACTIVE"
        self.soak_stop_reason = ""
        self.save(update_fields=["soak_status", "soak_stop_reason", "updated_at"])
        return True, "target_scoped_safety_hold_reconciled"

    def trigger_safety_stop(self, reason: str):
        """Immediately halts the bounded soak upon encountering safety-critical incidents or provider uncertainty.
        Preserves user-configured automation preferences (master, prospecting, connections, jobs, etc.)
        while securely pausing all provider execution via soak_status='SAFETY_STOP' and provider_safety_state."""
        self.soak_status = "SAFETY_STOP"
        self.soak_stop_reason = reason
        if "high_volume" in reason.lower() or "data_access" in reason.lower():
            self.provider_safety_state = "HIGH_VOLUME_DATA_ACCESS_WARNING"
        elif "checkpoint" in reason.lower() or "challenge" in reason.lower() or "captcha" in reason.lower() or "auth_redirect" in reason.lower():
            self.provider_safety_state = "CHECKPOINT_REQUIRED"
        elif "restrict" in reason.lower():
            self.provider_safety_state = "ACCOUNT_RESTRICTED"
        self.save(update_fields=[
            "soak_status",
            "soak_stop_reason",
            "provider_safety_state",
            "updated_at",
        ])

    def all_eight_switches_on(self) -> bool:
        """Returns True if all eight user-facing automation preferences are enabled."""
        return all([
            self.master_automation_enabled,
            self.auto_find_prospects,
            self.auto_send_connections,
            self.auto_followup,
            self.auto_find_jobs,
            self.auto_apply_jobs,
            self.auto_send_emails,
            self.auto_monitor_replies,
        ])

    def record_heartbeat(self):
        from django.utils import timezone
        self.last_daemon_heartbeat = timezone.now()
        self.save(update_fields=["last_daemon_heartbeat", "updated_at"])

    def is_heartbeat_healthy(self, threshold_seconds: int = 180) -> bool:
        if not self.last_daemon_heartbeat:
            return False
        from django.utils import timezone
        return (timezone.now() - self.last_daemon_heartbeat).total_seconds() < threshold_seconds

    def get_channel_runtime_status(self, channel_name: str) -> str:
        """
        Computes truthful runtime status separating ENABLED from ACTIVE/HEALTHY/DEGRADED/BLOCKED/QUOTA.
        Returns: RUNNING, IDLE_HEALTHY, DEGRADED, BLOCKED, TARGET_REACHED, or PAUSED.
        """
        from django.utils import timezone
        now = timezone.now()

        if self.soak_status == "SAFETY_STOP" or self.provider_safety_state == "HIGH_VOLUME_DATA_ACCESS_WARNING":
            return "SAFETY_STOP"

        if not self.master_automation_enabled:
            return "PAUSED"

        if channel_name == "jobs":
            if not self.auto_apply_jobs:
                return "PAUSED"
            from openoutreach.core.services.quota_service import get_daily_successful_easy_apply_count, get_daily_job_provider_attempt_count
            succ = get_daily_successful_easy_apply_count(workspace=self.workspace)
            attempts = get_daily_job_provider_attempt_count(workspace=self.workspace)
            if succ >= (self.daily_job_application_target or 50):
                return "TARGET_REACHED"
            if attempts >= (self.max_daily_job_applications or 60):
                return "QUOTA_REACHED"
            if self.last_job_application_attempt_at and (now - self.last_job_application_attempt_at).total_seconds() < 1800:
                return "RUNNING"
            if self.last_job_discovery_completed_at and (now - self.last_job_discovery_completed_at).total_seconds() < 1800:
                return "RUNNING"
            if self.last_job_discovery_completed_at and (now - self.last_job_discovery_completed_at).total_seconds() < 7200:
                return "IDLE_HEALTHY"
            return "DEGRADED"

        elif channel_name == "connections":
            if not self.auto_send_connections:
                return "PAUSED"
            from openoutreach.core.services.quota_service import get_daily_connection_provider_attempt_count
            attempts = get_daily_connection_provider_attempt_count(workspace=self.workspace)
            if attempts >= (self.max_daily_connections or 40):
                return "QUOTA_REACHED"
            if self.last_connection_attempt_at and (now - self.last_connection_attempt_at).total_seconds() < 1800:
                return "RUNNING"
            from openoutreach.crm.models import Deal, DealState
            ready_count = Deal.objects.filter(campaign__workspace=self.workspace, state=DealState.READY_TO_CONNECT, lead__disqualified=False).count()
            if ready_count == 0:
                if not self.last_prospect_discovery_at or (now - self.last_prospect_discovery_at).total_seconds() >= 1800:
                    return "DEGRADED"
            if self.last_connection_planner_at and (now - self.last_connection_planner_at).total_seconds() < 1800:
                return "IDLE_HEALTHY"
            return "DEGRADED"

        elif channel_name == "followups":
            if not self.auto_followup:
                return "PAUSED"
            from openoutreach.core.services.quota_service import get_daily_linkedin_outbound_message_breakdown
            breakdown = get_daily_linkedin_outbound_message_breakdown(workspace=self.workspace)
            if breakdown.get("total_aggregate", 0) >= (self.max_daily_messages or 90):
                return "QUOTA_REACHED"
            if self.last_followup_planner_at and (now - self.last_followup_planner_at).total_seconds() < 1800:
                return "IDLE_HEALTHY"
            return "DEGRADED"

        elif channel_name == "replies":
            if not self.auto_monitor_replies:
                return "PAUSED"
            if self.last_reply_poll_at and (now - self.last_reply_poll_at).total_seconds() < 1800:
                return "IDLE_HEALTHY"
            return "DEGRADED"

        elif channel_name == "acceptance":
            if not self.auto_send_connections:
                return "PAUSED"
            if self.last_acceptance_poll_at and (now - self.last_acceptance_poll_at).total_seconds() < 1800:
                return "RUNNING"
            return "DEGRADED"

        elif channel_name == "email":
            if not self.auto_send_emails:
                return "PAUSED"
            return "IDLE_HEALTHY"

        return "IDLE_HEALTHY"

    def get_location_targeting_policy(self):
        from openoutreach.core.services.location_targeting import LocationTargetingPolicy
        inc = [loc.strip() for loc in (self.default_outreach_locations or "").split(",") if loc.strip()]
        exc = [loc.strip() for loc in (self.default_outreach_locations_excluded or "").split(",") if loc.strip()]
        return LocationTargetingPolicy(
            included_locations=inc,
            excluded_locations=exc,
            unknown_location_handling=self.default_unknown_location_handling or "deny",
            channel="outreach",
        )

    @classmethod
    def get_for_workspace(cls, workspace: Workspace) -> WorkspaceAutomationSettings:
        if not workspace:
            return None
        settings, _ = cls.objects.get_or_create(workspace=workspace)
        return settings


class UsageMeter(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="usage_meters")
    metric_name = models.CharField(max_length=100)
    count = models.IntegerField(default=0)
    billing_period_start = models.DateTimeField()
    billing_period_end = models.DateTimeField()

    objects = TenantQuerySet.as_manager()

    class Meta:
        verbose_name = "Usage Meter"
        verbose_name_plural = "Usage Meters"
        unique_together = [("workspace", "metric_name", "billing_period_start")]

    def __str__(self):
        return f"[{self.workspace.slug}] {self.metric_name}: {self.count}"


class WorkspaceInvitation(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("revoked", "Revoked"),
        ("expired", "Expired"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(max_length=30, choices=WorkspaceMembership.ROLE_CHOICES, default="campaign_manager")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    token_hash_version = models.CharField(max_length=16, default="v1")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    invited_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="sent_invitations")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        verbose_name = "Workspace Invitation"
        verbose_name_plural = "Workspace Invitations"

    def __str__(self):
        return f"Invite for {self.email} -> {self.workspace.slug} [{self.role}] ({self.status})"


class PasswordResetToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="password_reset_tokens")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    token_hash_version = models.CharField(max_length=16, default="v1")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Password Reset Token"
        verbose_name_plural = "Password Reset Tokens"

    def __str__(self):
        return f"Password Reset Token for {self.user.username}"


class EmailVerificationToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="email_verification_tokens")
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    token_hash_version = models.CharField(max_length=16, default="v1")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Email Verification Token"
        verbose_name_plural = "Email Verification Tokens"

    def __str__(self):
        return f"Email Verification for {self.email} ({self.user.username})"


class OperatorActionApproval(models.Model):
    """
    Durable, exact one-time operator approval record.
    Allows an explicit single canary action (e.g. Job #X) when master automation is paused.
    Must be bound to exact workspace, candidate, job/external_job_id, task/draft, and consumed upon execution.
    """
    STATUS_CHOICES = [
        ("approved", "Approved"),
        ("claimed", "Claimed"),
        ("consumed", "Consumed"),
        ("failed", "Failed"),
        ("expired", "Expired"),
        ("revoked", "Revoked"),
    ]
    ACTION_TYPE_CHOICES = [
        ("job_application", "Job Application"),
        ("connect", "LinkedIn Connection"),
        ("follow_up", "LinkedIn Follow-up"),
        ("email", "Cold Email"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="operator_approvals")
    operator = models.ForeignKey(User, on_delete=models.PROTECT, related_name="operator_approvals")
    linkedin_profile = models.ForeignKey("linkedin.LinkedInProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="operator_approvals")
    lead = models.ForeignKey("crm.Lead", on_delete=models.CASCADE, null=True, blank=True, related_name="operator_approvals")
    deal = models.ForeignKey("crm.Deal", on_delete=models.CASCADE, null=True, blank=True, related_name="operator_approvals")
    message_hash = models.CharField(max_length=64, blank=True, default="")
    action_type = models.CharField(max_length=50, choices=ACTION_TYPE_CHOICES, default="job_application")
    candidate = models.ForeignKey("career.CandidateProfile", on_delete=models.CASCADE, null=True, blank=True, related_name="operator_approvals")
    job = models.ForeignKey("career.Job", on_delete=models.CASCADE, null=True, blank=True, related_name="operator_approvals")
    external_job_id = models.CharField(max_length=100, blank=True, default="")
    draft_id = models.IntegerField(null=True, blank=True)
    task = models.ForeignKey("core.Task", on_delete=models.SET_NULL, null=True, blank=True, related_name="operator_approvals")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="approved")
    approved_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "Operator Action Approval"
        verbose_name_plural = "Operator Action Approvals"

    def is_valid_for(
        self,
        *,
        workspace=None,
        linkedin_profile=None,
        candidate=None,
        job=None,
        lead=None,
        deal=None,
        task=None,
        draft_id=None,
        message_text=None,
        action_type="job_application"
    ) -> bool:
        """Check if this approval is active, unconsumed, unexpired, and strictly matches the targeted action and provider account."""
        if self.status not in ["approved", "claimed"]:
            return False
        if self.consumed_at is not None:
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        if self.action_type != action_type:
            return False
        if workspace and self.workspace_id != workspace.id:
            return False
        if self.linkedin_profile_id:
            if not linkedin_profile:
                return False
            prof_id = linkedin_profile.id if hasattr(linkedin_profile, "id") else linkedin_profile
            if self.linkedin_profile_id != prof_id:
                return False
        if linkedin_profile and hasattr(linkedin_profile, "user_id") and self.operator_id:
            if linkedin_profile.user_id != self.operator_id:
                return False
        if self.lead_id:
            if not lead:
                return False
            lead_id = lead.id if hasattr(lead, "id") else lead
            if self.lead_id != lead_id:
                return False
        if self.deal_id:
            if not deal:
                return False
            deal_id = deal.id if hasattr(deal, "id") else deal
            if self.deal_id != deal_id:
                return False
        if self.message_hash:
            if not message_text:
                return False
            import hashlib
            norm = str(message_text).strip().encode("utf-8")
            if hashlib.sha256(norm).hexdigest() != self.message_hash:
                return False
        if candidate and self.candidate_id and self.candidate_id != candidate.id:
            return False
        if job:
            if self.job_id and self.job_id != job.id:
                return False
            ext_id = getattr(job, "external_job_id", "")
            if self.external_job_id and ext_id and self.external_job_id != ext_id:
                return False
        if task and self.task_id and self.task_id != task.id:
            return False
        if draft_id and self.draft_id and self.draft_id != draft_id:
            return False
        return True

    @classmethod
    def claim_for_execution(
        cls,
        approval_id: int,
        *,
        workspace=None,
        linkedin_profile=None,
        candidate=None,
        job=None,
        lead=None,
        deal=None,
        task=None,
        draft_id=None,
        message_text=None,
        action_type="job_application"
    ) -> tuple[bool, Optional[OperatorActionApproval], str]:
        """
        Atomically claims an approved one-time record with select_for_update inside transaction.atomic.
        Guarantees that two workers can NEVER both claim or use the same one-time approval.
        """
        from django.db import transaction, OperationalError
        try:
            with transaction.atomic():
                approval = cls.objects.select_for_update().filter(id=approval_id).first()
                if not approval:
                    return False, None, "approval_not_found"
                if approval.status != "approved" or approval.consumed_at is not None or approval.claimed_at is not None:
                    return False, approval, f"approval_already_{approval.status}"
                if approval.expires_at and timezone.now() > approval.expires_at:
                    approval.status = "expired"
                    approval.save(update_fields=["status"])
                    return False, approval, "approval_expired"

                if not approval.is_valid_for(
                    workspace=workspace,
                    linkedin_profile=linkedin_profile,
                    candidate=candidate,
                    job=job,
                    lead=lead,
                    deal=deal,
                    task=task,
                    draft_id=draft_id,
                    message_text=message_text,
                    action_type=action_type
                ):
                    return False, approval, "approval_binding_mismatch"

                approval.status = "claimed"
                approval.claimed_at = timezone.now()
                approval.save(update_fields=["status", "claimed_at"])
                return True, approval, "claimed"
        except OperationalError:
            return False, None, "approval_lock_conflict"

    def mark_failed(self, reason: str = ""):
        """Mark claimed approval as failed (requiring explicit operator re-approval, no automatic retry)."""
        self.status = "failed"
        self.failure_reason = reason
        self.save(update_fields=["status", "failure_reason"])

    def consume(self):
        """Mark this one-time approval as consumed upon successful completion."""
        if self.status in ["approved", "claimed"] and not self.consumed_at:
            self.status = "consumed"
            self.consumed_at = timezone.now()
            self.save(update_fields=["status", "consumed_at"])

    @classmethod
    def reconcile_stale_approvals(cls, workspace=None) -> int:
        """
        Reconcile and expire stale, unconsumed, or orphaned OperatorActionApproval records.
        Transitions unclaimed or claimed unconsumed approvals to 'expired' or 'failed' so
        they cannot authorize future provider actions.
        """
        from django.db.models import Q
        now = timezone.now()
        qs = cls.objects.all()
        if workspace:
            qs = qs.filter(workspace=workspace)

        stale_qs = qs.filter(
            Q(status__in=["approved", "claimed"]) &
            (
                Q(expires_at__lt=now) |
                Q(expires_at__isnull=True, approved_at__lt=now - timezone.timedelta(hours=2)) |
                Q(status="claimed", claimed_at__lt=now - timezone.timedelta(minutes=30))
            )
        )
        count = 0
        for app in stale_qs:
            app.status = "failed" if app.status == "claimed" else "expired"
            app.failure_reason = "reconciled_stale_canary_approval"
            app.save(update_fields=["status", "failure_reason"])
            count += 1
        return count


class TargetingConversation(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="targeting_conversations")
    campaign = models.ForeignKey(Campaign, on_delete=models.SET_NULL, null=True, blank=True, related_name="targeting_conversations")
    title = models.CharField(max_length=255, default="Targeting Assistant")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Targeting Conversation: {self.title} ({self.workspace.slug})"


class TargetingMessage(models.Model):
    conversation = models.ForeignKey(TargetingConversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=[("operator", "Operator"), ("assistant", "Assistant"), ("system", "System")])
    content = models.TextField()
    structured_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"[{self.role}] {self.content[:40]}"


class TargetingConfigVersion(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="targeting_versions")
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, null=True, blank=True, related_name="targeting_versions")
    version_number = models.PositiveIntegerField(default=1)
    config_type = models.CharField(max_length=50, choices=[("prospect_icp", "Prospect ICP"), ("job_target", "Job Target")])
    config_data = models.JSONField(default=dict)
    operator_prompt = models.TextField(blank=True, default="")
    ai_summary = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_number"]
        unique_together = [("workspace", "campaign", "config_type", "version_number")]

    def __str__(self):
        camp_name = self.campaign.name if self.campaign else "Global"
        return f"{self.config_type} v{self.version_number} for {camp_name} ({'Active' if self.is_active else 'Superseded'})"


class StageCanaryBudget(models.Model):
    """
    Durable execution budget for controlled resume stages (e.g. Stage 1 Canary).
    
    Guarantees:
    1. Atomic claiming via database-backed locks (select_for_update) scoped by (workspace, linkedin_profile, stage).
    2. Once state reaches NETWORK_ATTEMPTED, COMPLETED, or BLOCKED, the budget is permanently consumed.
    3. Maximum provider read budget per canary interaction to prevent unbounded provider page loads.
    4. Process restarts, worker failovers, or script re-runs fail closed without re-authorizing live retries.
    """
    class State(models.TextChoices):
        NOT_STARTED = "NOT_STARTED", "Not Started"
        CLAIMED = "CLAIMED", "Claimed"
        NETWORK_ATTEMPTED = "NETWORK_ATTEMPTED", "Network Attempted"
        COMPLETED = "COMPLETED", "Completed"
        BLOCKED = "BLOCKED", "Blocked"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="stage_canary_budgets")
    linkedin_profile = models.ForeignKey(
        "linkedin.LinkedInProfile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="stage_canary_budgets"
    )
    stage = models.CharField(max_length=64, default="STAGE_1")
    state = models.CharField(max_length=32, choices=State.choices, default=State.NOT_STARTED)
    max_canary_attempts = models.PositiveIntegerField(default=1)
    attempts_count = models.PositiveIntegerField(default=0)
    max_provider_reads = models.PositiveIntegerField(default=5)
    provider_reads_count = models.PositiveIntegerField(default=0)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_task_id = models.PositiveBigIntegerField(null=True, blank=True)
    attempted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_outcome = models.CharField(max_length=128, blank=True, default="")
    last_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("workspace", "linkedin_profile", "stage")]
        indexes = [
            models.Index(fields=["workspace", "stage", "state"]),
        ]

    def __str__(self):
        prof_id = self.linkedin_profile_id or "Any"
        return f"StageCanaryBudget ws={self.workspace_id} prof={prof_id} stage={self.stage} state={self.state} attempts={self.attempts_count}/{self.max_canary_attempts}"

    @classmethod
    def claim_canary(
        cls,
        workspace,
        linkedin_profile=None,
        stage="STAGE_1",
        task_id=None,
        max_attempts=1,
        max_reads=5
    ) -> tuple[bool, Optional[StageCanaryBudget], str]:
        """Atomically claim execution budget for the specified resume stage."""
        from django.db import transaction, OperationalError
        try:
            with transaction.atomic():
                budget, _ = cls.objects.select_for_update().get_or_create(
                    workspace=workspace,
                    linkedin_profile=linkedin_profile,
                    stage=stage,
                    defaults={
                        "max_canary_attempts": max_attempts,
                        "max_provider_reads": max_reads,
                        "state": cls.State.NOT_STARTED,
                    }
                )
                if budget.state in [cls.State.NETWORK_ATTEMPTED, cls.State.COMPLETED, cls.State.BLOCKED] or budget.attempts_count >= budget.max_canary_attempts:
                    return False, budget, f"canary_budget_exhausted (state={budget.state}, attempts={budget.attempts_count}/{budget.max_canary_attempts})"
                if budget.state == cls.State.CLAIMED and budget.claimed_task_id and task_id and budget.claimed_task_id != task_id:
                    return False, budget, f"canary_already_claimed by task #{budget.claimed_task_id}"

                budget.state = cls.State.CLAIMED
                budget.claimed_at = timezone.now()
                if task_id:
                    budget.claimed_task_id = task_id
                budget.save(update_fields=["state", "claimed_at", "claimed_task_id", "updated_at"])
                return True, budget, "claimed"
        except OperationalError:
            return False, None, "canary_budget_lock_conflict"

    def record_network_attempt(self, task_id=None):
        """Record that live provider network navigation has started. Consumes budget permanently."""
        self.state = self.State.NETWORK_ATTEMPTED
        self.attempts_count += 1
        self.attempted_at = timezone.now()
        if task_id:
            self.claimed_task_id = task_id
        self.save(update_fields=["state", "attempts_count", "attempted_at", "claimed_task_id", "updated_at"])

    def record_provider_read(self, count=1) -> bool:
        """Track provider read and enforce max_provider_reads limit."""
        self.provider_reads_count += count
        if self.provider_reads_count > self.max_provider_reads:
            self.state = self.State.BLOCKED
            self.last_reason = f"max_provider_reads_exceeded ({self.provider_reads_count} > {self.max_provider_reads})"
            self.save(update_fields=["state", "provider_reads_count", "last_reason", "updated_at"])
            return False
        self.save(update_fields=["provider_reads_count", "updated_at"])
        return True

    def finalize_canary(self, outcome: str, reason: str = ""):
        """Finalize canary execution outcome."""
        if outcome in ["submitted", "externally_confirmed"]:
            self.state = self.State.COMPLETED
        elif self.state != self.State.BLOCKED:
            self.state = self.State.NETWORK_ATTEMPTED
        self.last_outcome = str(outcome)[:128]
        self.last_reason = str(reason)
        self.completed_at = timezone.now()
        self.save(update_fields=["state", "last_outcome", "last_reason", "completed_at", "updated_at"])







