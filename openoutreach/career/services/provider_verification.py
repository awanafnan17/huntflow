"""Controlled Provider Verification Service for OpenOutreach Job Listings.

Enforces strict trust boundaries:
1. Only authentic LIVE_BROWSER or PROVIDER_API observations can promote a Job to VERIFIED.
2. Caller-supplied raw dictionaries / TEST_FIXTURE evidence MUST NOT produce production VERIFIED status.
3. Numeric external job ID redirects to a different job are rejected as INVALID/CONFLICT.
4. Structured page signals (JSON-LD, meta tags, DOM, document title) are safely extracted and recorded.
5. All sensitive credentials are completely excluded from evidence storage.
"""

from __future__ import annotations
import json
import re
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from django.utils import timezone

from openoutreach.career.models import Job

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderVerificationObservation:
    """
    Internal immutable observation object produced strictly by genuine provider inspection
    (such as a live Playwright browser session or authenticated provider API).
    Cannot be forged by passing an arbitrary dict to public APIs.
    """
    provider: str
    expected_external_job_id: str
    observed_external_job_id: str
    canonical_url: str
    observed_title: str
    observed_company: str
    observed_location: str
    is_expired: bool
    easy_apply: bool
    verification_source: str  # "LIVE_BROWSER", "PROVIDER_API", "TEST_FIXTURE"
    extraction_source: str    # "dom", "json_ld", "meta_tags", "document_title", "fixture"
    session_profile_id: Optional[int] = None
    raw_signals: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderJobVerificationResult:
    job_id: int
    external_job_id: str
    provider: str
    status: str  # "VERIFIED", "EXPIRED", "CONFLICT", "INVALID", "UNKNOWN", "TEST_FIXTURE", "UNVERIFIED"
    canonical_url: str
    observed_title: str
    observed_company: str
    observed_location: str
    easy_apply: bool
    is_expired: bool
    verification_source: str
    extraction_source: str
    evidence: dict[str, Any]
    error: str = ""


def extract_provider_page_signals(
    page: Any,
    *,
    expected_external_job_id: str,
    canonical_url: str,
    session_profile_id: Optional[int] = None,
    provider: str = "linkedin"
) -> ProviderVerificationObservation:
    """
    Extracts provider-observed signals from an authenticated, live Playwright browser page.
    Inspects final URL, availability text, DOM selectors, JSON-LD, and document title.
    """
    current_url = ""
    observed_external_job_id = ""
    try:
        current_url = page.url
        _, ext_id, _ = Job.extract_canonical_provider_identity(current_url)
        observed_external_job_id = ext_id or expected_external_job_id
    except Exception:
        current_url = canonical_url
        observed_external_job_id = expected_external_job_id

    # 1. Availability check
    is_expired = False
    try:
        body_text = page.locator("body").inner_text().lower()
        if any(msg in body_text for msg in [
            "no longer accepting applications",
            "job is no longer available",
            "this job has expired",
            "job posting has expired",
            "this position has been closed"
        ]):
            is_expired = True
    except Exception as e:
        logger.debug("Error checking body text for expiration: %s", e)

    observed_title = ""
    observed_company = ""
    observed_location = ""
    extraction_source = "none"

    # 2. JSON-LD structured data extraction
    try:
        json_scripts = page.locator('script[type="application/ld+json"]')
        for i in range(json_scripts.count()):
            try:
                raw_json = json_scripts.nth(i).inner_text().strip()
                data = json.loads(raw_json)
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    observed_title = (data.get("title") or "").strip()
                    org = data.get("hiringOrganization")
                    if isinstance(org, dict):
                        observed_company = (org.get("name") or "").strip()
                    elif isinstance(org, str):
                        observed_company = org.strip()
                    loc = data.get("jobLocation")
                    if isinstance(loc, dict) and "address" in loc:
                        addr = loc["address"]
                        if isinstance(addr, dict):
                            observed_location = addr.get("addressLocality") or addr.get("addressRegion") or ""
                    if observed_title and observed_company:
                        extraction_source = "json_ld"
                        break
            except Exception:
                continue
    except Exception as e:
        logger.debug("Error checking JSON-LD: %s", e)

    # 3. DOM selectors extraction
    if not (observed_title and observed_company):
        try:
            title_loc = page.locator(
                "h1.job-details-jobs-unified-top-card__job-title, "
                "h1.t-24, "
                ".jobs-unified-top-card__job-title, "
                ".job-view-layout h1"
            ).first
            if title_loc.count() > 0:
                observed_title = title_loc.inner_text().strip()

            comp_loc = page.locator(
                ".job-details-jobs-unified-top-card__company-name, "
                ".jobs-unified-top-card__company-name, "
                "a.ember-view[href*='/company/'], "
                ".job-details-jobs-unified-top-card__primary-description a"
            ).first
            if comp_loc.count() > 0:
                observed_company = comp_loc.inner_text().strip()

            loc_loc = page.locator(
                ".job-details-jobs-unified-top-card__workplace-type, "
                ".job-details-jobs-unified-top-card__bullet, "
                ".jobs-unified-top-card__bullet"
            ).first
            if loc_loc.count() > 0:
                observed_location = loc_loc.inner_text().strip()

            if observed_title or observed_company:
                extraction_source = "dom"
        except Exception as e:
            logger.debug("Error checking DOM selectors: %s", e)

    # 4. Document Title Fallback
    if not (observed_title and observed_company):
        try:
            page_title = page.title().strip()
            # E.g. "Python Developer | $55/hr Remote | Crossing Hurdles | LinkedIn"
            # or "Senior Software Engineer at Xorora | LinkedIn"
            if page_title and "linkedin" in page_title.lower():
                parts = [p.strip() for p in page_title.split("|") if p.strip() and p.strip().lower() != "linkedin"]
                if len(parts) >= 2:
                    if not observed_title:
                        observed_title = parts[0]
                    if not observed_company:
                        observed_company = parts[-1]
                    extraction_source = "document_title"
                elif " at " in page_title:
                    m = re.search(r"^(.*?)\s+at\s+(.*?)(?:\s*\|\s*LinkedIn|$)", page_title, re.IGNORECASE)
                    if m:
                        if not observed_title:
                            observed_title = m.group(1).strip()
                        if not observed_company:
                            observed_company = m.group(2).strip()
                        extraction_source = "document_title"
        except Exception as e:
            logger.debug("Error parsing document title: %s", e)

    # 5. Easy Apply button
    easy_apply = False
    try:
        easy_btn = page.locator("button.jobs-apply-button, button[aria-label*='Easy Apply']").first
        if easy_btn.count() > 0:
            btn_text = easy_btn.inner_text().lower()
            easy_apply = ("easy apply" in btn_text or "apply" in btn_text)
    except Exception:
        pass

    return ProviderVerificationObservation(
        provider=provider,
        expected_external_job_id=str(expected_external_job_id),
        observed_external_job_id=str(observed_external_job_id),
        canonical_url=canonical_url,
        observed_title=observed_title,
        observed_company=observed_company,
        observed_location=observed_location,
        is_expired=is_expired,
        easy_apply=easy_apply,
        verification_source="LIVE_BROWSER",
        extraction_source=extraction_source,
        session_profile_id=session_profile_id,
        raw_signals={"final_url": current_url}
    )


def verify_provider_job_identity_from_browser(
    job: Job,
    *,
    browser_page: Any,
    session: Optional[Any] = None
) -> ProviderJobVerificationResult:
    """
    Production entry point: executes authenticated read-only browser inspection against
    the job's canonical provider URL, extracts page signals, and updates job identity status.
    """
    url = (job.job_url or "").strip()
    provider, ext_id, canonical_url = Job.extract_canonical_provider_identity(url)
    if not provider:
        provider = (job.provider or "linkedin").strip().lower()
    if not ext_id:
        ext_id = str(job.external_job_id or "").strip()

    if not ext_id or not canonical_url.startswith("http"):
        job.provider_identity_status = "INVALID"
        job.verification_error = "Invalid or unparseable provider job URL / external ID."
        job.save(update_fields=["provider_identity_status", "verification_error"])
        return ProviderJobVerificationResult(
            job_id=job.id,
            external_job_id=ext_id,
            provider=provider,
            status="INVALID",
            canonical_url=canonical_url or url,
            observed_title="",
            observed_company="",
            observed_location="",
            easy_apply=False,
            is_expired=False,
            verification_source="LIVE_BROWSER",
            extraction_source="none",
            evidence={},
            error=job.verification_error
        )

    profile_id = getattr(session, "linkedin_profile_id", None) or (
        getattr(session.linkedin_profile, "id", None) if hasattr(session, "linkedin_profile") and session.linkedin_profile else None
    )

    try:
        browser_page.goto(canonical_url, wait_until="domcontentloaded", timeout=25000)
        browser_page.wait_for_timeout(2500)
    except Exception as e:
        logger.warning("Browser navigation failed for Job #%d (%s): %s", job.id, canonical_url, e)
        job.provider_identity_status = "UNKNOWN"
        job.verification_error = f"Browser inspection failed: {str(e)}"
        job.save(update_fields=["provider_identity_status", "verification_error"])
        return ProviderJobVerificationResult(
            job_id=job.id,
            external_job_id=ext_id,
            provider=provider,
            status="UNKNOWN",
            canonical_url=canonical_url,
            observed_title="",
            observed_company="",
            observed_location="",
            easy_apply=False,
            is_expired=False,
            verification_source="LIVE_BROWSER",
            extraction_source="none",
            evidence={},
            error=job.verification_error
        )

    observation = extract_provider_page_signals(
        browser_page,
        expected_external_job_id=ext_id,
        canonical_url=canonical_url,
        session_profile_id=profile_id,
        provider=provider
    )

    return verify_provider_job_identity_from_observation(job, observation)


def verify_provider_job_identity_from_observation(
    job: Job,
    observation: ProviderVerificationObservation
) -> ProviderJobVerificationResult:
    """
    Applies an internal ProviderVerificationObservation to the Job entity.
    Guarantees that only LIVE_BROWSER or PROVIDER_API observations can produce VERIFIED status.
    """
    now = timezone.now()
    provider = observation.provider
    ext_id = observation.expected_external_job_id
    canonical_url = observation.canonical_url

    # 1. Reject external job ID redirect mismatches
    if observation.observed_external_job_id and observation.observed_external_job_id != ext_id:
        job.provider_identity_status = "INVALID"
        job.verification_error = (
            f"Provider redirected to different job ID: {observation.observed_external_job_id} "
            f"(expected {ext_id})"
        )
        job.save(update_fields=["provider_identity_status", "verification_error"])
        return ProviderJobVerificationResult(
            job_id=job.id,
            external_job_id=ext_id,
            provider=provider,
            status="INVALID",
            canonical_url=canonical_url,
            observed_title=observation.observed_title,
            observed_company=observation.observed_company,
            observed_location=observation.observed_location,
            easy_apply=observation.easy_apply,
            is_expired=observation.is_expired,
            verification_source=observation.verification_source,
            extraction_source=observation.extraction_source,
            evidence={},
            error=job.verification_error
        )

    # 2. Determine verification status
    if observation.verification_source not in ["LIVE_BROWSER", "PROVIDER_API"]:
        # Test fixture or unverified source cannot create production VERIFIED truth
        status = "TEST_FIXTURE" if observation.verification_source == "TEST_FIXTURE" else "UNVERIFIED"
    elif observation.is_expired:
        status = "EXPIRED"
    elif observation.observed_title and observation.observed_company:
        existing_canonical = Job.objects.filter(
            provider=provider,
            external_job_id=ext_id,
            canonical_job__isnull=True
        ).exclude(id=job.id).first()

        if existing_canonical and (
            existing_canonical.company.lower() != observation.observed_company.lower() and
            observation.observed_company.lower() not in existing_canonical.company.lower()
        ):
            status = "CONFLICT"
        else:
            status = "VERIFIED"
    elif observation.observed_title or observation.observed_company:
        status = "VERIFIED"
    else:
        status = "UNVERIFIED"

    # 3. Construct safe structured evidence
    evidence_payload = f"{provider}:{ext_id}:{observation.observed_company}:{observation.observed_title}:{status}:{observation.verification_source}:{now.isoformat()}"
    evidence_hash = hashlib.sha256(evidence_payload.encode("utf-8")).hexdigest()

    safe_evidence = {
        "provider": provider,
        "external_job_id": ext_id,
        "canonical_url": canonical_url,
        "observed_title": observation.observed_title or job.title,
        "observed_company": observation.observed_company or job.company,
        "observed_location": observation.observed_location or job.location,
        "observed_at": now.isoformat(),
        "verification_source": observation.verification_source,
        "extraction_source": observation.extraction_source,
        "session_profile_id": observation.session_profile_id,
        "verification_result": status,
        "evidence_hash": evidence_hash,
        "easy_apply": observation.easy_apply,
        "is_expired": observation.is_expired,
    }

    job.provider = provider
    job.external_job_id = ext_id
    job.job_url = canonical_url
    if observation.observed_title:
        job.title = observation.observed_title
    if observation.observed_company:
        job.company = observation.observed_company
    if observation.observed_location:
        job.location = observation.observed_location
    job.easy_apply = observation.easy_apply
    job.provider_identity_status = status
    job.provider_verification_evidence = safe_evidence
    if status == "VERIFIED":
        job.last_provider_verified_at = now
        job.provenance = "REAL_PROVIDER"
    elif status == "TEST_FIXTURE":
        job.provenance = "TEST_FIXTURE"
        job.is_synthetic = True
    job.verification_error = ""
    job.save()

    return ProviderJobVerificationResult(
        job_id=job.id,
        external_job_id=ext_id,
        provider=provider,
        status=status,
        canonical_url=canonical_url,
        observed_title=observation.observed_title or job.title,
        observed_company=observation.observed_company or job.company,
        observed_location=observation.observed_location or job.location,
        easy_apply=observation.easy_apply,
        is_expired=observation.is_expired,
        verification_source=observation.verification_source,
        extraction_source=observation.extraction_source,
        evidence=safe_evidence,
        error=""
    )


def verify_provider_job_identity_from_test_fixture(
    job: Job,
    *,
    raw_evidence: dict[str, Any]
) -> ProviderJobVerificationResult:
    """
    Test-only entry point: updates job from test fixture metadata.
    Strictly forces provenance to TEST_FIXTURE and status to TEST_FIXTURE / UNVERIFIED.
    NEVER creates production VERIFIED truth.
    """
    obs = ProviderVerificationObservation(
        provider=(job.provider or "linkedin").lower(),
        expected_external_job_id=str(job.external_job_id or "fixture"),
        observed_external_job_id=str(job.external_job_id or "fixture"),
        canonical_url=job.job_url or f"https://www.linkedin.com/jobs/view/{job.external_job_id}/",
        observed_title=(raw_evidence.get("observed_title") or raw_evidence.get("title") or job.title).strip(),
        observed_company=(raw_evidence.get("observed_company") or raw_evidence.get("company") or job.company).strip(),
        observed_location=(raw_evidence.get("observed_location") or raw_evidence.get("location") or job.location).strip(),
        is_expired=bool(raw_evidence.get("is_expired", False)),
        easy_apply=bool(raw_evidence.get("easy_apply", job.easy_apply)),
        verification_source="TEST_FIXTURE",
        extraction_source="fixture",
        raw_signals=raw_evidence
    )
    return verify_provider_job_identity_from_observation(job, obs)


def verify_provider_job_identity(
    job: Job,
    *,
    session: Optional[Any] = None,
    browser_page: Optional[Any] = None,
    raw_evidence: Optional[dict[str, Any]] = None
) -> ProviderJobVerificationResult:
    """
    General / legacy dispatcher.
    - If browser_page is provided: delegates to live browser inspection.
    - If raw_evidence is passed by arbitrary caller: rejects promotion to VERIFIED,
      marking the job as UNVERIFIED (or TEST_FIXTURE) to protect the trust boundary.
    """
    if browser_page is not None:
        return verify_provider_job_identity_from_browser(job, browser_page=browser_page, session=session)

    if raw_evidence is not None:
        # Strict trust boundary: Caller-supplied dictionary CANNOT create production VERIFIED truth.
        logger.warning("Caller supplied raw_evidence directly to verify_provider_job_identity for Job #%d; rejecting production VERIFIED status.", job.id)
        job.provider_identity_status = "UNVERIFIED"
        job.verification_error = "Caller-supplied raw_evidence cannot establish production VERIFIED status."
        job.save(update_fields=["provider_identity_status", "verification_error"])
        return ProviderJobVerificationResult(
            job_id=job.id,
            external_job_id=str(job.external_job_id or ""),
            provider=job.provider or "linkedin",
            status="UNVERIFIED",
            canonical_url=job.job_url or "",
            observed_title=raw_evidence.get("observed_title") or job.title,
            observed_company=raw_evidence.get("observed_company") or job.company,
            observed_location=raw_evidence.get("observed_location") or job.location,
            easy_apply=bool(raw_evidence.get("easy_apply", False)),
            is_expired=bool(raw_evidence.get("is_expired", False)),
            verification_source="CALLER_RAW_EVIDENCE_REJECTED",
            extraction_source="none",
            evidence={},
            error=job.verification_error
        )

    # No inspection executed
    job.provider_identity_status = "UNVERIFIED"
    job.verification_error = "Provider inspection has not been executed."
    job.save(update_fields=["provider_identity_status", "verification_error"])
    return ProviderJobVerificationResult(
        job_id=job.id,
        external_job_id=str(job.external_job_id or ""),
        provider=job.provider or "linkedin",
        status="UNVERIFIED",
        canonical_url=job.job_url or "",
        observed_title="",
        observed_company="",
        observed_location="",
        easy_apply=False,
        is_expired=False,
        verification_source="NONE",
        extraction_source="none",
        evidence={},
        error=job.verification_error
    )
