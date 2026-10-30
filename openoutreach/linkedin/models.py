# openoutreach/linkedin/models.py
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Tuple

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from django.core.validators import MinValueValidator

from openoutreach.core.models import Campaign

logger = logging.getLogger(__name__)

# action_type → daily_limit_field
_RATE_LIMIT_FIELDS = {
    "connect": "connect_daily_limit",
    "follow_up": "follow_up_daily_limit",
}


class LinkedInProfile(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="linkedin_profiles",
    )
    workspace = models.ForeignKey(
        "core.Workspace",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="linkedin_profiles",
    )
    self_lead = models.ForeignKey(
        "crm.Lead",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    linkedin_username = models.CharField(max_length=200)
    linkedin_password = models.TextField(blank=True, default="")
    subscribe_newsletter = models.BooleanField(default=True)
    # Operator opt-in to give back to the central contacts store at all — the
    # whole contribution (emails and, when cached, the profile vector for the
    # agentic-email-marketing product; linkedin-docs
    # roadmap/p1-e3-agentic-email-marketing-product.md). Not asked at onboarding —
    # set from the operator's LinkedIn country at first daemon run by
    # ``apply_gdpr_contribution_override`` (keyed to ``is_eea_located``, not the
    # EEA/UK/CH, off inside it (or unknown
    # location). Off = no give-back at all (and so no give-to-get credits / no
    # resolve). Either way the raw profile text never leaves the operator's
    # machine — only the vector.
    contribute_to_hub = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    connect_daily_limit = models.PositiveIntegerField(
        null=True, blank=True, default=20, validators=[MinValueValidator(1)]
    )
    follow_up_daily_limit = models.PositiveIntegerField(
        null=True, blank=True, default=25, validators=[MinValueValidator(1)]
    )
    legal_accepted = models.BooleanField(default=False)
    cookie_data = models.JSONField(null=True, blank=True)
    session_status = models.CharField(
        max_length=50,
        choices=[
            ("valid", "Valid"),
            ("invalid", "Invalid"),
            ("expired", "Expired"),
            ("checkpoint", "Checkpoint / Challenge"),
            ("restricted", "Restricted Account"),
            ("needs_reauth", "Needs Re-authentication"),
            ("unknown", "Unknown"),
        ],
        default="unknown",
    )
    last_validated_at = models.DateTimeField(null=True, blank=True)
    last_authenticated_at = models.DateTimeField(null=True, blank=True)
    session_error_details = models.TextField(null=True, blank=True)
    newsletter_processed = models.BooleanField(default=False)
    display_name = models.CharField(max_length=200, blank=True, default="")
    public_profile_url = models.CharField(max_length=500, blank=True, default="")
    public_identifier = models.CharField(max_length=200, blank=True, default="")
    identity_validated_at = models.DateTimeField(null=True, blank=True)
    identity_validation_status = models.CharField(max_length=50, default="unvalidated")

    SESSION_ORIGIN_CHOICES = [
        ("imported_seed", "Imported Session Seed"),
        ("huntflow_managed", "HuntFlow Managed Session"),
    ]
    session_origin = models.CharField(
        max_length=50,
        choices=SESSION_ORIGIN_CHOICES,
        default="imported_seed",
        help_text="Provenance of provider session credentials.",
    )
    bootstrap_completed_at = models.DateTimeField(null=True, blank=True)
    last_server_state_persisted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(connect_daily_limit__isnull=True) | models.Q(connect_daily_limit__gte=1),
                name="linkedinprofile_connect_daily_limit_check"
            ),
            models.CheckConstraint(
                condition=models.Q(follow_up_daily_limit__isnull=True) | models.Q(follow_up_daily_limit__gte=1),
                name="linkedinprofile_follow_up_daily_limit_check"
            ),
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._exhausted: dict[str, date] = {}

    def can_execute(self, action_type: str = "connect") -> bool:
        """Check if the action is allowed under session validity and daily rate limit."""
        if self.session_status in ["needs_reauth", "invalid", "expired", "checkpoint", "restricted", "unknown"]:
            return False
        if self.identity_validation_status in ["UNKNOWN_IDENTITY", "invalid_mismatch", "unvalidated"]:
            return False

        # Reset exhaustion flag on a new day
        exhausted_date = self._exhausted.get(action_type)
        if exhausted_date is not None and exhausted_date != date.today():
            del self._exhausted[action_type]
        if action_type in self._exhausted:
            return False

        daily_field = _RATE_LIMIT_FIELDS.get(action_type)
        if daily_field:
            self.refresh_from_db(fields=[daily_field])
            daily_limit = getattr(self, daily_field)
            if daily_limit is not None and self._daily_count(action_type) >= daily_limit:
                return False

        return True

    def record_action(self, action_type: str, campaign: Campaign, metadata: dict | None = None) -> None:
        """Persist a rate-limited action with optional audit metadata."""
        ActionLog.objects.create(
            linkedin_profile=self, campaign=campaign,
            action_type=action_type, metadata=metadata,
        )

    def mark_exhausted(self, action_type: str) -> None:
        """Mark the action type as externally exhausted for today."""
        self._exhausted[action_type] = date.today()
        logger.warning("Rate limit: %s externally exhausted for today", action_type)

    def _daily_count(self, action_type: str) -> int:
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return ActionLog.objects.filter(
            linkedin_profile=self, action_type=action_type,
            created_at__gte=today_start,
        ).count()

    def save(self, *args, **kwargs):
        from openoutreach.core.encryption import encrypt_string, encrypt_dict
        if self.linkedin_password and not self.linkedin_password.startswith("v1::") and not self.linkedin_password.startswith("gcm_enc::"):
            self.linkedin_password = encrypt_string(self.linkedin_password)
        if self.cookie_data:
            if isinstance(self.cookie_data, dict):
                self.cookie_data = encrypt_dict(self.cookie_data)
            elif isinstance(self.cookie_data, str) and not self.cookie_data.startswith("v1::") and not self.cookie_data.startswith("gcm_enc::"):
                self.cookie_data = encrypt_string(self.cookie_data)
        super().save(*args, **kwargs)

    def get_decrypted_password(self) -> str:
        from openoutreach.core.encryption import decrypt_string
        return decrypt_string(self.linkedin_password) if self.linkedin_password else ""

    def get_decrypted_cookie_data(self) -> Optional[dict]:
        from openoutreach.core.encryption import decrypt_dict
        return decrypt_dict(self.cookie_data) if self.cookie_data else None

    def set_password(self, plaintext: str) -> None:
        from openoutreach.core.encryption import encrypt_string
        self.linkedin_password = encrypt_string(plaintext) if plaintext else ""

    def set_cookie_data(self, data: Optional[dict | str]) -> None:
        from openoutreach.core.encryption import encrypt_dict, encrypt_string
        if isinstance(data, dict):
            self.cookie_data = encrypt_dict(data)
        elif isinstance(data, str):
            self.cookie_data = encrypt_string(data)
        else:
            self.cookie_data = None

    def import_session_seed(self, raw_li_at: str) -> bool:
        """Store and encrypt an imported session seed (legacy/recovery path).
        
        Refuses to silently overwrite a HuntFlow-managed server session.
        Explicit replacement requires replace_managed_session_with_imported_seed.
        """
        from openoutreach.linkedin.exceptions import SessionOwnershipConflictError
        if self.session_origin == "huntflow_managed":
            raise SessionOwnershipConflictError(
                "Cannot silently overwrite a HuntFlow-managed server session with an imported session seed. "
                "Explicit operator replacement confirmation required."
            )

        if not raw_li_at or not str(raw_li_at).strip():
            raise ValueError("Please enter a valid li_at session cookie.")

        cleaned_li_at = str(raw_li_at).strip()
        if "li_at=" in cleaned_li_at:
            cleaned_li_at = cleaned_li_at.split("li_at=")[-1].split(";")[0].strip()
        if not cleaned_li_at:
            raise ValueError("Please enter a valid li_at session cookie.")

        existing_state = self.get_decrypted_cookie_data()
        if isinstance(existing_state, dict) and isinstance(existing_state.get("cookies"), list) and len(existing_state["cookies"]) > 0:
            storage_state = existing_state
            cookies = storage_state["cookies"]
            updated = False
            for c in cookies:
                if isinstance(c, dict) and c.get("name") == "li_at":
                    c["value"] = cleaned_li_at
                    updated = True
            if not updated:
                cookies.append({
                    "name": "li_at",
                    "value": cleaned_li_at,
                    "domain": ".linkedin.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                })
            storage_state["version"] = 1
        else:
            storage_state = {
                "version": 1,
                "cookies": [
                    {
                        "name": "li_at",
                        "value": cleaned_li_at,
                        "domain": ".www.linkedin.com",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "None",
                    },
                    {
                        "name": "li_at",
                        "value": cleaned_li_at,
                        "domain": ".linkedin.com",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "None",
                    },
                ],
                "origins": [],
            }

        self.set_cookie_data(storage_state)
        self.session_origin = "imported_seed"
        self.session_status = "needs_reauth"
        self.session_error_details = "Session cookie saved securely (imported seed). Verification required."
        self.last_authenticated_at = timezone.now()
        self.save(update_fields=["cookie_data", "session_status", "session_error_details", "last_authenticated_at", "session_origin"])
        logger.info("Saved imported session seed for profile ID %d with zero provider network", self.pk)
        return True

    def replace_managed_session_with_imported_seed(self, raw_li_at: str, confirmation: str = "") -> bool:
        """Explicitly replaces a HuntFlow-managed session with an imported session seed."""
        if confirmation != "REPLACE_MANAGED_SESSION":
            raise ValueError(
                "Explicit confirmation 'REPLACE_MANAGED_SESSION' required to replace a HuntFlow-managed session."
            )

        if not raw_li_at or not str(raw_li_at).strip():
            raise ValueError("Please enter a valid li_at session cookie.")

        cleaned_li_at = str(raw_li_at).strip()
        if "li_at=" in cleaned_li_at:
            cleaned_li_at = cleaned_li_at.split("li_at=")[-1].split(";")[0].strip()
        if not cleaned_li_at:
            raise ValueError("Please enter a valid li_at session cookie.")

        storage_state = {
            "version": 1,
            "cookies": [
                {
                    "name": "li_at",
                    "value": cleaned_li_at,
                    "domain": ".www.linkedin.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                },
                {
                    "name": "li_at",
                    "value": cleaned_li_at,
                    "domain": ".linkedin.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                },
            ],
            "origins": [],
        }

        self.set_cookie_data(storage_state)
        self.session_origin = "imported_seed"
        self.session_status = "needs_reauth"
        self.session_error_details = "Managed session explicitly replaced with imported session seed. Verification required."
        self.last_authenticated_at = timezone.now()
        self.save(update_fields=["cookie_data", "session_status", "session_error_details", "last_authenticated_at", "session_origin"])
        logger.info("Explicitly replaced managed session with imported seed for profile ID %d", self.pk)
        return True

    def persist_managed_storage_state(self, storage_state: dict, caller_lease_token: Optional[str] = None) -> bool:
        """Internal trusted persistence path for HuntFlow-managed server session state.
        
        Requirements:
        - Must be called only from trusted server browser lifecycle code
        - Validates structural integrity (must contain cookies list and li_at cookie)
        - Stamps version: 1
        - Preserves/sets session_origin = huntflow_managed
        - Updates last_server_state_persisted_at
        - Does NOT require a user-facing force flag
        """
        if not isinstance(storage_state, dict):
            raise ValueError("storage_state must be a dictionary")

        cookies = storage_state.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            raise ValueError("storage_state must contain a non-empty cookies list")

        has_li_at = any(
            isinstance(c, dict) and c.get("name") == "li_at" and c.get("value")
            for c in cookies
        )
        if not has_li_at:
            raise ValueError("storage_state missing required li_at authentication cookie")

        if "version" not in storage_state:
            storage_state["version"] = 1

        self.set_cookie_data(storage_state)
        self.session_origin = "huntflow_managed"
        self.last_server_state_persisted_at = timezone.now()
        self.save(update_fields=["cookie_data", "session_origin", "last_server_state_persisted_at"])
        logger.info("Persisted trusted server-managed storage state for profile ID %d", self.pk)
        return True

    def __str__(self):
        uname = getattr(self.user, "username", "") if getattr(self, "user", None) else ""
        return f"{uname} ({self.linkedin_username})"

    def __repr__(self):
        uname = getattr(self.user, "username", "") if getattr(self, "user", None) else ""
        return f"<LinkedInProfile id={self.id} user={uname} status={self.session_status} password=[REDACTED] cookies=[REDACTED]>"

    def safe_dict(self):
        """Return diagnostic dictionary with sensitive credentials redacted."""
        from openoutreach.core.utils.security import safe_model_repr
        return safe_model_repr(self)


class SearchKeyword(models.Model):
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="search_keywords",
    )
    keyword = models.CharField(max_length=500)
    used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("campaign", "keyword")]

    def __str__(self):
        return self.keyword


class ActionLog(models.Model):
    class ActionType(models.TextChoices):
        CONNECT = "connect", "Connect"
        FOLLOW_UP = "follow_up", "Follow Up"

    linkedin_profile = models.ForeignKey(
        LinkedInProfile,
        on_delete=models.CASCADE,
        related_name="action_logs",
    )
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="action_logs",
    )
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    metadata = models.JSONField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["linkedin_profile", "action_type", "created_at"]),
        ]

    def __str__(self):
        return f"{self.action_type} by {self.linkedin_profile} at {self.created_at}"


class ConnectionAttempt(models.Model):
    STATUS_CHOICES = [
        ("prepared", "Prepared"),
        ("attempting", "Attempting"),
        ("externally_confirmed", "Externally Confirmed"),
        ("uncertain", "Uncertain"),
        ("indeterminate", "Indeterminate"),
        ("failed_before_provider", "Failed Before Provider"),
        ("reconciliation_required", "Reconciliation Required"),
        ("failed", "Failed"),
    ]
    linkedin_profile = models.ForeignKey(
        LinkedInProfile,
        on_delete=models.CASCADE,
        related_name="connection_attempts"
    )
    recipient_public_id = models.CharField(max_length=200)
    idempotency_key = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="prepared")
    note_text = models.TextField(blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"ConnectionAttempt {self.status} to {self.recipient_public_id} ({self.idempotency_key[:8]})"


class ConnectionChildAttempt(models.Model):
    parent = models.ForeignKey(ConnectionAttempt, on_delete=models.CASCADE, related_name="child_attempts")
    sequence = models.PositiveIntegerField()
    timestamp = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    recipient = models.CharField(max_length=200)
    attempted_url = models.CharField(max_length=500)
    outcome = models.CharField(max_length=50) # prepared, failed, externally_confirmed, indeterminate
    error_classification = models.CharField(max_length=100, blank=True)
    exception_type = models.CharField(max_length=100, blank=True)
    externally_sent = models.BooleanField(default=False)
    is_retrospective = models.BooleanField(default=False)

    def __str__(self):
        return f"ChildAttempt #{self.sequence} ({self.outcome}) for parent {self.parent_id}"


class ProviderOperationType(models.TextChoices):
    JOB_SEARCH = "job_search", "Job Search"
    JOB_DETAIL = "job_detail", "Job Detail"
    PROSPECT_SEARCH = "prospect_search", "Prospect Search"
    PROFILE_DETAIL = "profile_detail", "Profile Detail"
    INBOX = "inbox", "Inbox"
    PENDING_INVITATIONS = "pending_invitations", "Pending Invitations"
    CONNECTION_UI = "connection_ui", "Connection UI"
    EASY_APPLY = "easy_apply", "Easy Apply"
    SESSION_HEALTH = "session_health", "Session Health"
    OTHER = "other", "Other"


class ProviderReadLedger(models.Model):
    linkedin_profile = models.ForeignKey(
        LinkedInProfile,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="provider_reads",
    )
    workspace = models.ForeignKey(
        "core.Workspace",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="provider_reads",
    )
    operation_type = models.CharField(
        max_length=40,
        choices=ProviderOperationType.choices,
        default=ProviderOperationType.OTHER,
    )
    resource_identifier = models.CharField(max_length=255, db_index=True)
    cache_hit = models.BooleanField(default=False)
    success = models.BooleanField(default=True)
    read_cost = models.PositiveIntegerField(default=1)
    duration_ms = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["operation_type", "created_at"]),
            models.Index(fields=["resource_identifier", "created_at"]),
            models.Index(fields=["cache_hit", "created_at"]),
        ]
        verbose_name = "Provider Read Ledger Entry"
        verbose_name_plural = "Provider Read Ledger Entries"

    def __str__(self):
        return f"{self.operation_type} ({'CACHE_HIT' if self.cache_hit else 'LIVE_READ'}) [{self.resource_identifier}] at {self.created_at}"


class ProviderEvidenceCache(models.Model):
    RESOURCE_TYPE_CHOICES = [
        ("profile", "Profile"),
        ("job_search", "Job Search"),
        ("job_detail", "Job Detail"),
        ("easy_apply", "Easy Apply"),
        ("connection_state", "Connection State"),
        ("inbox", "Inbox"),
        ("pending_invitations", "Pending Invitations"),
    ]
    resource_type = models.CharField(max_length=40, choices=RESOURCE_TYPE_CHOICES, db_index=True)
    resource_identifier = models.CharField(max_length=255, db_index=True)
    linkedin_profile = models.ForeignKey(
        LinkedInProfile,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="evidence_cache",
    )
    data = models.JSONField(default=dict)
    observed_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    is_valid = models.BooleanField(default=True)

    class Meta:
        unique_together = [("resource_type", "resource_identifier", "linkedin_profile")]
        indexes = [
            models.Index(fields=["resource_type", "resource_identifier", "expires_at"]),
        ]
        verbose_name = "Provider Evidence Cache Entry"
        verbose_name_plural = "Provider Evidence Cache Entries"

    def is_fresh(self) -> bool:
        return self.is_valid and timezone.now() < self.expires_at

    def __str__(self):
        return f"Cache [{self.resource_type}:{self.resource_identifier}] valid={self.is_valid} expires={self.expires_at}"


class ProviderSafetyEvent(models.Model):
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.CASCADE,
        related_name="provider_safety_events",
    )
    linkedin_profile = models.ForeignKey(
        LinkedInProfile,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="safety_events",
    )
    event_type = models.CharField(max_length=60, db_index=True)
    url_category = models.CharField(max_length=255, blank=True)
    action_taken = models.CharField(max_length=60, default="SAFETY_STOP")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "event_type", "created_at"]),
            models.Index(fields=["linkedin_profile", "created_at"]),
        ]
        verbose_name = "Provider Safety Event"
        verbose_name_plural = "Provider Safety Events"

    def __str__(self):
        return f"SafetyEvent [{self.event_type}] for ws={self.workspace_id} profile={self.linkedin_profile_id} at {self.created_at}"


class ProviderWriteIntent(models.Model):
    """
    Durable write intent and idempotency record for all LinkedIn mutating actions.
    Tracks state through PLANNED -> CLAIMED -> PROVIDER_ATTEMPTED -> CONFIRMED / UNCERTAIN / FAILED.
    """
    class State(models.TextChoices):
        PLANNED = "planned", "Planned"
        CLAIMED = "claimed", "Claimed"
        PROVIDER_ATTEMPTED = "provider_attempted", "Provider Attempted"
        CONFIRMED = "confirmed", "Confirmed"
        UNCERTAIN = "uncertain", "Uncertain"
        FAILED_BEFORE_PROVIDER = "failed_before_provider", "Failed Before Provider"
        FAILED = "failed", "Failed"

    class OperationType(models.TextChoices):
        CONNECTION_SEND = "connection_send", "Connection Send"
        FIRST_MESSAGE_SEND = "first_message_send", "First Message Send"
        FOLLOW_UP_SEND = "follow_up_send", "Follow Up Send"
        AI_REPLY_SEND = "ai_reply_send", "AI Reply Send"
        EASY_APPLY_SUBMIT = "easy_apply_submit", "Easy Apply Submit"
        OTHER = "other", "Other"

    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.CASCADE,
        related_name="provider_write_intents",
    )
    linkedin_profile = models.ForeignKey(
        LinkedInProfile,
        on_delete=models.CASCADE,
        related_name="write_intents",
    )
    operation_type = models.CharField(max_length=50, choices=OperationType.choices)
    target_identity = models.CharField(max_length=255)
    logical_action_identity = models.CharField(max_length=255, blank=True, default="")
    idempotency_key = models.CharField(max_length=128, unique=True)
    state = models.CharField(max_length=50, choices=State.choices, default=State.PLANNED)
    provider_confirmation = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    attempted_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "linkedin_profile", "operation_type", "state"]),
            models.Index(fields=["idempotency_key"]),
        ]
        verbose_name = "Provider Write Intent"
        verbose_name_plural = "Provider Write Intents"

    def __str__(self):
        return f"WriteIntent [{self.operation_type}:{self.target_identity}] ({self.state})"


class ProviderAccountLease(models.Model):
    """
    Cross-process serialization lease for active provider write/read operations.
    Prevents concurrent worker collisions while providing automatic crash expiry.
    """
    linkedin_profile = models.OneToOneField(
        LinkedInProfile,
        on_delete=models.CASCADE,
        related_name="account_lease",
    )
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.CASCADE,
        related_name="account_leases",
    )
    locked_by = models.CharField(max_length=100)
    owner_token = models.CharField(max_length=64, blank=True, default="")
    acquired_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    active_operation = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        verbose_name = "Provider Account Lease"
        verbose_name_plural = "Provider Account Leases"

    def __str__(self):
        return f"Lease for profile={self.linkedin_profile_id} by {self.locked_by} expires={self.expires_at}"


class ProviderRelationship(models.Model):
    """
    Durable, first-class provider relationship state for LinkedIn prospects.
    Maintains provider-observed relationship truth separately from CRM Deal state.
    """
    class Classification(models.TextChoices):
        CONNECTED = "CONNECTED", "Connected"
        PENDING = "PENDING", "Pending"
        NOT_CONNECTED = "NOT_CONNECTED", "Not Connected"
        UNKNOWN = "UNKNOWN", "Unknown"
        SAFETY_BLOCKED = "SAFETY_BLOCKED", "Safety Blocked"
        AUTH_REQUIRED = "AUTH_REQUIRED", "Auth Required"

    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.CASCADE,
        related_name="provider_relationships",
    )
    linkedin_profile = models.ForeignKey(
        LinkedInProfile,
        null=False,
        blank=False,
        on_delete=models.CASCADE,
        related_name="provider_relationships",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.CASCADE,
        related_name="provider_relationships",
    )
    deal = models.ForeignKey(
        "crm.Deal",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="provider_relationships",
    )
    classification = models.CharField(
        max_length=40,
        choices=Classification.choices,
        default=Classification.UNKNOWN,
        db_index=True,
    )
    observed_degree = models.IntegerField(null=True, blank=True)
    evidence = models.TextField(blank=True, default="")
    evidence_sufficient = models.BooleanField(default=False)
    last_observed_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_read_ledger = models.ForeignKey(
        ProviderReadLedger,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    consecutive_unknown_count = models.IntegerField(default=0)
    next_eligible_reconciliation_at = models.DateTimeField(null=True, blank=True, db_index=True)
    operator_review_required = models.BooleanField(default=False)
    last_safety_signal = models.CharField(max_length=100, default="NONE")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("workspace", "linkedin_profile", "lead")]
        indexes = [
            models.Index(fields=["workspace", "linkedin_profile", "lead"]),
            models.Index(fields=["workspace", "lead", "classification"]),
            models.Index(fields=["next_eligible_reconciliation_at"]),
        ]
        verbose_name = "Provider Relationship"
        verbose_name_plural = "Provider Relationships"

    def __str__(self):
        return f"ProviderRelationship [{self.lead_id}:{self.classification}] ws={self.workspace_id} sufficient={self.evidence_sufficient}"

    def is_connection_fresh(self, max_age_days: int = 7) -> bool:
        """
        Check if connection evidence is positive, sufficient, and fresh.
        """
        if self.classification != self.Classification.CONNECTED:
            return False
        if not self.evidence_sufficient:
            return False
        if not self.last_observed_at:
            return False
        cutoff = timezone.now() - timedelta(days=max_age_days)
        return self.last_observed_at >= cutoff

    def can_first_message(self, max_age_days: int = 7) -> Tuple[bool, str]:
        """
        Validate whether relationship state authorizes FIRST_MESSAGE.
        """
        if self.classification != self.Classification.CONNECTED:
            return False, f"provider_relationship_{self.classification.lower()}"
        if not self.evidence_sufficient:
            return False, "insufficient_relationship_evidence"
        if not self.is_connection_fresh(max_age_days=max_age_days):
            return False, "stale_relationship_evidence"
        return True, "OK"


class ServerBootstrapSession(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        ABORTED = "aborted", "Aborted"
        EXPIRED = "expired", "Expired"
        FAILED = "failed", "Failed"

    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.CASCADE,
        related_name="server_bootstrap_sessions",
    )
    linkedin_profile = models.ForeignKey(
        LinkedInProfile,
        on_delete=models.CASCADE,
        related_name="server_bootstrap_sessions",
    )
    operator = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="server_bootstrap_sessions",
    )
    token_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 hash of single-use bootstrap authorization token.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    lease_token = models.CharField(max_length=100, blank=True, default="")
    viewer_connected = models.BooleanField(default=False)
    viewer_attached_at = models.DateTimeField(null=True, blank=True)
    viewer_connection_id = models.CharField(max_length=64, blank=True, default="")
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    aborted_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "Server Bootstrap Session"
        verbose_name_plural = "Server Bootstrap Sessions"
        ordering = ["-created_at"]

    def __str__(self):
        return f"<ServerBootstrapSession id={self.id} profile_id={self.linkedin_profile_id} status={self.status}>"


# Alias for compatibility
ProviderRelationshipState = ProviderRelationship


if not hasattr(User, "linkedin_profile"):
    def _get_user_linkedin_profile(self):
        return self.linkedin_profiles.first()
    User.linkedin_profile = property(_get_user_linkedin_profile)


