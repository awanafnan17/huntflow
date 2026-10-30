import logging

import numpy as np
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from openoutreach.core.models import TenantQuerySet

logger = logging.getLogger(__name__)


def extract_name_company_from_facts(facts: list[str]) -> tuple[str, str, str]:
    """Helper to extract first_name, last_name, and company from a list of facts.
    Returns (first_name, last_name, company).
    """
    import re
    first_name = ""
    last_name = ""
    company = ""
    
    for fact in facts:
        fact_clean = fact.strip().rstrip(".")
        fact_lower = fact_clean.lower()
        
        # Name
        if "first name is " in fact_lower:
            parts = re.split(r"first name is ", fact_clean, flags=re.IGNORECASE)
            if len(parts) > 1:
                first_name = parts[1].strip()
        elif "last name is " in fact_lower:
            parts = re.split(r"last name is ", fact_clean, flags=re.IGNORECASE)
            if len(parts) > 1:
                last_name = parts[1].strip()
                
        # Company
        if not company:
            if "works at " in fact_lower:
                parts = re.split(r"works at ", fact_clean, flags=re.IGNORECASE)
                if len(parts) > 1:
                    company = parts[1].strip()
            elif "employed by " in fact_lower:
                parts = re.split(r"employed by ", fact_clean, flags=re.IGNORECASE)
                if len(parts) > 1:
                    company = parts[1].strip()
            elif "is a " in fact_lower and " at " in fact_lower:
                parts = re.split(r" at ", fact_clean, flags=re.IGNORECASE)
                if len(parts) > 1:
                    company = parts[-1].strip()
                    
    return first_name, last_name, company


class Lead(models.Model):
    objects = TenantQuerySet.as_manager()

    class Meta:
        verbose_name = _("Lead")
        verbose_name_plural = _("Leads")

    linkedin_url = models.URLField(max_length=200, unique=True)
    public_identifier = models.CharField(max_length=200, unique=True)
    urn = models.CharField(max_length=200, null=True, blank=True, unique=True, db_index=True)
    # ISO-3166 alpha-2 of the lead's current location, cached from the profile
    # scrape. Drives the contacts-store geo-gate; blank = unknown (→ never contributed).
    country_code = models.CharField(max_length=2, blank=True, default="")
    embedding = models.BinaryField(null=True, blank=True)
    
    # Profile Identity Fields (Batch 5.9)
    first_name = models.CharField(max_length=255, blank=True, default="")
    last_name = models.CharField(max_length=255, blank=True, default="")
    headline = models.CharField(max_length=1000, blank=True, default="")
    current_title = models.CharField(max_length=255, blank=True, default="")
    company = models.CharField(max_length=255, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    industry = models.CharField(max_length=255, blank=True, default="")
    is_synthetic = models.BooleanField(default=False)
    synthetic_run = models.ForeignKey(
        "core.SyntheticDemonstrationRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="leads",
    )

    # Email enrichment — one field per source (roadmap: p1-e1 storage decision):
    #   contact_info — raw LinkedIn contact-info overlay {email, emails, phone_numbers},
    #                  captured once at CONNECTED; null = never scraped (idempotency flag).
    #   api_email    — enrichment-API result (BetterContact); its writer lands with the
    #                  finder slice (p1-e3). null = not found.
    contact_info = models.JSONField(null=True, blank=True, default=None)
    api_email = models.EmailField(null=True, blank=True, default=None)
    disqualified = models.BooleanField(default=False)
    workspace = models.ForeignKey(
        "core.Workspace",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="leads",
    )

    def update_identity_from_profile(self, profile: dict, save: bool = True):
        """Populate identity fields from a LinkedIn profile dict (Batch 5.9)."""
        if not profile:
            return
        
        # Check both outer level and inner "profile" sub-dict
        p = profile.get("profile") or {}
        if not isinstance(p, dict):
            p = {}

        fn = profile.get("first_name") or p.get("first_name") or ""
        ln = profile.get("last_name") or p.get("last_name") or ""
        hl = profile.get("headline") or p.get("headline") or ""
        loc = profile.get("location_name") or p.get("location_name") or profile.get("location") or p.get("location") or ""
        
        # Industry
        ind_raw = profile.get("industry") or p.get("industry") or {}
        ind = ""
        if isinstance(ind_raw, dict):
            ind = ind_raw.get("name") or ""
        elif ind_raw:
            ind = str(ind_raw)

        # Positions
        positions = profile.get("positions") or p.get("positions") or []
        title = ""
        comp = ""
        if positions and isinstance(positions, list):
            first_pos = positions[0]
            if isinstance(first_pos, dict):
                title = first_pos.get("title") or ""
                comp = first_pos.get("company_name") or ""

        # Update if blank
        if fn and not self.first_name:
            self.first_name = fn
        if ln and not self.last_name:
            self.last_name = ln
        if hl and not self.headline:
            self.headline = hl
        if title and not self.current_title:
            self.current_title = title
        if comp and not self.company:
            self.company = comp
        if loc and not self.location:
            self.location = loc
        if ind and not self.industry:
            self.industry = ind

        if save:
            self.save(update_fields=[
                "first_name", "last_name", "headline", "current_title",
                "company", "location", "industry"
            ])

    def get_bettercontact_fields(self) -> dict[str, str]:
        """Retrieve reliably stored first_name, last_name, and company.
        No guessing or parsing public_identifier.
        """
        # Prioritize new DB fields if populated
        first_name = self.first_name
        last_name = self.last_name
        company = self.company

        # Fallback to contact_info JSON if populated
        if not first_name and self.contact_info:
            first_name = self.contact_info.get("first_name") or ""
        if not last_name and self.contact_info:
            last_name = self.contact_info.get("last_name") or ""
        if not company and self.contact_info:
            company = self.contact_info.get("company") or ""

        # Fallback to related Deal's profile_summary facts
        deal = self.deal_set.first()
        if deal and deal.profile_summary and "facts" in deal.profile_summary:
            facts = deal.profile_summary["facts"]
            fn, ln, co = extract_name_company_from_facts(facts)
            if fn and not first_name:
                first_name = fn
            if ln and not last_name:
                last_name = ln
            if co and not company:
                company = co

        return {
            "first_name": first_name,
            "last_name": last_name,
            "company": company,
            "company_domain": "",  # Never invent domain
        }

    creation_date = models.DateTimeField(default=timezone.now)
    update_date = models.DateTimeField(auto_now=True)

    def __str__(self):
        label = self.public_identifier or self.linkedin_url or f"Lead#{self.pk}"
        if self.disqualified:
            return f"({_('Disqualified')}) {label}"
        return label

    # ------------------------------------------------------------------
    # Lazy accessors — re-scrape live on demand, persist only the
    # derived caches we still keep (urn, embedding).
    # ------------------------------------------------------------------

    def get_profile(self, session) -> dict | None:
        """Fetch profile dict using Central Evidence Cache -> Voyager live scrape.

        Checks provider safety and central cache before making any live network request.
        """
        from openoutreach.linkedin.services.evidence_cache import (
            PROFILE_STATIC_FACTS_TTL,
            get_cached_evidence,
            record_provider_read,
            set_cached_evidence,
        )
        from openoutreach.linkedin.services.account_governor import is_account_safe_for_provider_read
        from openoutreach.linkedin.models import ProviderOperationType
        from linkedin_cli.api.client import PlaywrightLinkedinAPI
        from linkedin_cli.exceptions import ProfileInaccessibleError

        pid = self.public_identifier or self.linkedin_url
        prof = getattr(session, "linkedin_profile", None)

        # 1. Check Central Evidence Cache
        cached = get_cached_evidence(
            resource_type="profile",
            resource_identifier=pid,
            linkedin_profile=prof,
        )
        if cached:
            record_provider_read(
                operation_type=ProviderOperationType.PROFILE_DETAIL,
                resource_identifier=pid,
                cache_hit=True,
                linkedin_profile=prof,
                workspace=self.workspace,
                success=True,
                read_cost=0,
            )
            return cached

        # 2. Check Account Safety Governor
        is_safe, safety_reason = is_account_safe_for_provider_read(
            workspace=self.workspace,
            linkedin_profile=prof,
        )
        if not is_safe:
            logger.warning("lead.get_profile blocked by governor: %s", safety_reason)
            return None

        session.ensure_browser()
        api = PlaywrightLinkedinAPI(session=session)
        try:
            profile, _raw = api.get_profile(public_identifier=self.public_identifier)
        except ProfileInaccessibleError:
            record_provider_read(
                operation_type=ProviderOperationType.PROFILE_DETAIL,
                resource_identifier=pid,
                cache_hit=False,
                linkedin_profile=prof,
                workspace=self.workspace,
                success=False,
                read_cost=1,
            )
            return None
        except Exception:
            record_provider_read(
                operation_type=ProviderOperationType.PROFILE_DETAIL,
                resource_identifier=pid,
                cache_hit=False,
                linkedin_profile=prof,
                workspace=self.workspace,
                success=False,
                read_cost=1,
            )
            raise

        if not profile:
            return None

        record_provider_read(
            operation_type=ProviderOperationType.PROFILE_DETAIL,
            resource_identifier=pid,
            cache_hit=False,
            linkedin_profile=prof,
            workspace=self.workspace,
            success=True,
            read_cost=1,
        )

        set_cached_evidence(
            resource_type="profile",
            resource_identifier=pid,
            data=profile,
            ttl=PROFILE_STATIC_FACTS_TTL,
            linkedin_profile=prof,
        )

        urn = profile.get("urn") or None
        if urn and self.urn != urn:
            if Lead.objects.filter(urn=urn).exclude(pk=self.pk).exists():
                logger.warning("URN %s already owned by another lead — skipping for %s", urn, self.public_identifier)
            else:
                self.urn = urn
                self.save(update_fields=["urn"])

        country = (profile.get("country_code") or "").strip().lower()
        if country and self.country_code != country:
            self.country_code = country
            self.save(update_fields=["country_code"])

        self.update_identity_from_profile(profile)

        return profile

    def capture_contact_info(self, session) -> None:
        """Scrape + persist the LinkedIn contact-info overlay for a 1st-degree
        connection.

        The stored value is a tri-state retry sentinel: ``None`` means we never
        got a clean read (never tried, or the fetch raised — the error path
        returns before the field is written), so a later visit retries; a non-null
        overlay — even an email-empty ``{email: None, emails: [], phone_numbers:
        []}`` — means the read succeeded and the member simply exposes no email,
        so it is not re-scraped. The raw overlay is stored unfiltered
        (work-vs-personal cleaning is downstream, in dbt).

        Errors are left to the caller: capture is driven from the CONNECTED
        transition (``set_profile_state``) and from each follow-up visit, both of
        which own the best-effort guard (``ProfileInaccessibleError``/``IOError``
        swallowed → field stays ``None`` → retried; ``AuthenticationError``
        propagates to the daemon's reauth handler).
        """
        if self.contact_info is not None:
            return
        from linkedin_cli.api.client import PlaywrightLinkedinAPI

        session.ensure_browser()
        api = PlaywrightLinkedinAPI(session=session)
        try:
            contact, _raw = api.get_contact_info(public_identifier=self.public_identifier)
            self.contact_info = contact
            self.save(update_fields=["contact_info"])
        except (TypeError, ValueError, KeyError, AttributeError):
            pass

    def resolve_api_email(self) -> bool | None:
        """Resolve + persist a work email via BetterContact, once the lead qualifies.

        Returns True on a hit (``api_email`` set, cached — never re-resolved →
        caller routes the Deal QUALIFIED → READY_TO_EMAIL), False on a genuine
        miss (BetterContact ran, found nothing → caller leaves the Deal QUALIFIED
        for the connect leg), and None when it couldn't run (no key, or the
        service was unreachable → caller leaves the Deal QUALIFIED to retry).
        A miss is free to retry — BetterContact bills only usable hits.
        """
        if self.api_email:
            logger.info("[%s] BetterContact check: email already resolved: %s", self.public_identifier, self.api_email)
            return True
        from openoutreach.emails.bettercontact import (
            BetterContactQuery,
            BetterContactUnavailable,
            resolve_email,
            is_configured,
        )
        from openoutreach.core.models import SiteConfig
        cfg = SiteConfig.load()
        api_key = cfg.bettercontact_api_key

        if not is_configured():
            logger.warning("[%s] BetterContact check: API key not configured.", self.public_identifier)
            return None

        # Gather reliable fields
        fields = self.get_bettercontact_fields()
        query = BetterContactQuery(
            linkedin_url=self.linkedin_url,
            first_name=fields.get("first_name", ""),
            last_name=fields.get("last_name", ""),
            company=fields.get("company", ""),
            company_domain=fields.get("company_domain", ""),
        )

        logger.info(
            "[%s] BetterContact lookup attempted (first_name=%r, last_name=%r, company=%r)",
            self.public_identifier, query.first_name, query.last_name, query.company
        )

        try:
            result = resolve_email(query)
        except BetterContactUnavailable as exc:
            err_msg = str(exc)
            if api_key:
                err_msg = err_msg.replace(api_key, "********")
            logger.error("[%s] BetterContact check: provider unavailable: %s", self.public_identifier, err_msg)
            return None
        except Exception as exc:
            err_msg = str(exc)
            if api_key:
                err_msg = err_msg.replace(api_key, "********")
            logger.error("[%s] BetterContact check: unexpected error during lookup: %s", self.public_identifier, err_msg)
            return None

        if result and result.email and "@" in result.email and "." in result.email.split("@")[-1]:
            self.api_email = result.email.strip()
            self.save(update_fields=["api_email"])
            logger.info("[%s] BetterContact check: email found: %s", self.public_identifier, self.api_email)
            return True

        logger.info("[%s] BetterContact check: no email found.", self.public_identifier)
        return False

    def get_urn(self, session) -> str:
        """LinkedIn URN. Reads cached column; falls back to a live scrape."""
        if self.urn:
            return self.urn
        self.get_profile(session)  # sets self.urn as side-effect
        if self.urn:
            return self.urn
        raise ValueError(f"Lead {self.pk}: could not resolve URN after re-fetch")

    def get_embedding(self, session) -> np.ndarray | None:
        """384-dim embedding. Lazy: scrapes + embeds on first access."""
        if self.embedding is None:
            profile = self.get_profile(session)
            if profile:
                self.embed_from_profile(profile)
        return self.embedding_array

    def embed_from_profile(self, profile: dict) -> None:
        """Compute and persist the 384-dim embedding from an in-hand profile.

        Used by callers that already have a freshly parsed profile dict,
        so they can skip the scrape that ``get_embedding`` would trigger.
        """
        from openoutreach.linkedin.ml.embeddings import embed_text
        from openoutreach.linkedin.ml.profile_text import build_profile_text

        text = build_profile_text({"profile": profile})
        emb = embed_text(text)
        self.embedding = emb.tobytes()
        self.save(update_fields=["embedding"])

    def to_profile_dict(self) -> dict:
        """Standard profile dict shape used by qualifiers and pools."""
        return {
            "lead_id": self.pk,
            "public_identifier": self.public_identifier,
            "url": self.linkedin_url or "",
            "first_name": self.first_name or "",
            "last_name": self.last_name or "",
            "current_title": self.current_title or "",
            "headline": self.headline or "",
            "company": self.company or "",
            "location": self.location or self.country_code or "",
            "country_code": self.country_code or "",
            "industry": self.industry or "",
            "meta": {},
        }

    @property
    def embedding_array(self) -> np.ndarray | None:
        """384-dim float32 numpy array from stored bytes, or None."""
        if self.embedding is None:
            return None
        return np.frombuffer(bytes(self.embedding), dtype=np.float32).copy()

    @embedding_array.setter
    def embedding_array(self, arr: np.ndarray):
        self.embedding = np.asarray(arr, dtype=np.float32).tobytes()

    @classmethod
    def get_labeled_arrays(cls, campaign) -> tuple[np.ndarray, np.ndarray]:
        """Labeled embeddings for a campaign as (X, y) numpy arrays for warm start.

        Labels are derived from Deal state and outcome:
        - label=1: Deals at any non-FAILED state (QUALIFIED and beyond)
        - label=0: FAILED Deals with outcome "wrong_fit" (LLM rejection)
        - Skipped: FAILED Deals with other outcomes (operational failures)
        """
        from openoutreach.crm.models import Outcome
        from openoutreach.crm.models.deal import Deal
        from openoutreach.crm.models import DealState

        deals = Deal.objects.filter(
            campaign=campaign, lead_id__isnull=False,
        ).values_list("lead_id", "state", "outcome")

        label_by_lead: dict[int, int] = {}
        for lid, state, outcome in deals:
            if state == DealState.FAILED:
                if outcome == Outcome.WRONG_FIT:
                    label_by_lead[lid] = 0
            else:
                label_by_lead[lid] = 1

        if not label_by_lead:
            return np.empty((0, 384), dtype=np.float32), np.empty(0, dtype=np.int32)

        leads_with_emb = dict(
            cls.objects.filter(pk__in=label_by_lead, embedding__isnull=False)
            .values_list("pk", "embedding")
        )

        X_list, y_list = [], []
        for lid, label in label_by_lead.items():
            emb = leads_with_emb.get(lid)
            if emb is None:
                continue
            X_list.append(np.frombuffer(bytes(emb), dtype=np.float32))
            y_list.append(label)

        if not X_list:
            return np.empty((0, 384), dtype=np.float32), np.empty(0, dtype=np.int32)

        return np.array(X_list, dtype=np.float32), np.array(y_list, dtype=np.int32)


class SuppressionRecord(models.Model):
    objects = TenantQuerySet.as_manager()

    linkedin_url = models.URLField(unique=True)
    reason = models.TextField(blank=True)
    workspace = models.ForeignKey(
        "core.Workspace",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="suppressions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Suppressed: {self.linkedin_url}"


class TargetImport(models.Model):
    STATUS_CHOICES = [
        ("uploaded", "Uploaded"),
        ("mapped", "Mapped"),
        ("validated", "Validated"),
        ("previewed", "Previewed"),
        ("confirmed", "Confirmed"),
        ("imported", "Imported"),
        ("partially_imported", "Partially Imported"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]

    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="target_imports")
    campaign = models.ForeignKey("core.Campaign", null=True, blank=True, on_delete=models.CASCADE, related_name="target_imports")
    name = models.CharField(max_length=200)
    source_type = models.CharField(max_length=50, default="csv")
    file_name = models.CharField(max_length=255)
    total_rows = models.PositiveIntegerField(default=0)
    imported_rows = models.PositiveIntegerField(default=0)
    failed_rows = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="uploaded")
    created_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    def __str__(self):
        return f"Import '{self.name}' [{self.status}]"


class TargetImportMapping(models.Model):
    target_import = models.OneToOneField(TargetImport, on_delete=models.CASCADE, related_name="mapping")
    column_mapping = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class TargetImportRow(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("valid", "Valid"),
        ("invalid", "Invalid"),
        ("duplicate", "Duplicate"),
        ("suppressed", "Suppressed"),
        ("imported", "Imported"),
    ]

    target_import = models.ForeignKey(TargetImport, on_delete=models.CASCADE, related_name="rows")
    row_index = models.PositiveIntegerField()
    raw_data = models.JSONField(default=dict)
    normalized_data = models.JSONField(default=dict, blank=True)
    validation_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    error_message = models.TextField(blank=True, default="")
    created_lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL)
    created_deal = models.ForeignKey("crm.Deal", null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["target_import", "row_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["target_import", "row_index"],
                name="unique_import_row_index",
            )
        ]

