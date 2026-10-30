"""
Multi-Source External Confirmation Evidence & Provider Verification Engine.
Handles multi-signal verification (post-submit modal, email confirmation, tracker, top-card)
with strict job-specific matching, 3-state verification (CONFIRMED_APPLIED, NOT_APPLIED, UNKNOWN),
and positive evidence precedence protection.
"""
import re
import hashlib
import logging
from typing import Dict, Any, Optional, Tuple, List
from django.utils import timezone

logger = logging.getLogger(__name__)


class ConfirmationEvidenceType:
    POST_SUBMIT_CONFIRMATION = "post_submit_confirmation"
    LINKEDIN_EMAIL = "linkedin_application_confirmation_email"
    OPERATOR_CONFIRMED_EMAIL = "operator_confirmed_email"
    OPERATOR_CONFIRMED_LINKEDIN_EMAIL = "operator_confirmed_linkedin_acknowledgement_email"
    LINKEDIN_TRACKER = "linkedin_applied_jobs_tracker"
    LINKEDIN_TOPCARD = "linkedin_job_topcard_applied"
    MANUAL_OPERATOR_AUDIT = "manual_operator_audit"


class VerificationStatus:
    CONFIRMED_APPLIED = "CONFIRMED_APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"


# Provenance-ranked list of strong positive evidence types
STRONG_POSITIVE_EVIDENCE = {
    ConfirmationEvidenceType.POST_SUBMIT_CONFIRMATION,
    ConfirmationEvidenceType.LINKEDIN_EMAIL,
    ConfirmationEvidenceType.OPERATOR_CONFIRMED_EMAIL,
    ConfirmationEvidenceType.OPERATOR_CONFIRMED_LINKEDIN_EMAIL,
    ConfirmationEvidenceType.LINKEDIN_TRACKER,
    ConfirmationEvidenceType.LINKEDIN_TOPCARD,
    ConfirmationEvidenceType.MANUAL_OPERATOR_AUDIT,
}


class VerificationResult(tuple):
    """
    Rich 3-tuple for verification results supporting (is_applied, evidence_type, metadata).
    Provides .is_applied, .evidence_type, .metadata, and .status (CONFIRMED_APPLIED, NOT_APPLIED, UNKNOWN).
    """
    def __new__(cls, is_applied: bool, evidence_type: str, metadata: Optional[Dict[str, Any]] = None, status: Optional[str] = None):
        meta = dict(metadata or {})
        if status is None:
            if is_applied:
                status = VerificationStatus.CONFIRMED_APPLIED
            elif evidence_type in ("not_applied_active_button_present", "not_applied_explicit"):
                status = VerificationStatus.NOT_APPLIED
            else:
                status = VerificationStatus.UNKNOWN
        meta["verification_status"] = status
        obj = super().__new__(cls, (is_applied, evidence_type, meta))
        obj.is_applied = is_applied
        obj.evidence_type = evidence_type
        obj.metadata = meta
        obj.status = status
        return obj


def verify_linkedin_application_email(
    email_metadata: Dict[str, Any],
    candidate,
    job,
    attempted_at: Optional[Any] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Validates a LinkedIn application confirmation email against candidate and job facts.
    
    Safety rules:
    - Sender must be verified LinkedIn (e.g. jobs-listings@linkedin.com, messages-noreply@linkedin.com, *@linkedin.com)
    - Subject/snippet must contain application confirmation semantics
    - Candidate association: recipient matches candidate email or user email
    - Job association: company name (case-insensitive substring match) or job title or external job ID
    - Never persists full email bodies, passwords, cookies, or secrets.
    """
    if not email_metadata:
        return False, "missing_email_metadata", {}

    sender = str(email_metadata.get("sender") or email_metadata.get("from", "")).lower().strip()
    subject = str(email_metadata.get("subject", "")).strip()
    recipient = str(email_metadata.get("recipient") or email_metadata.get("to", "")).lower().strip()
    received_at = email_metadata.get("received_at") or email_metadata.get("date")
    message_id = str(email_metadata.get("message_id", "")).strip()

    # 1. Verified LinkedIn Sender / Source
    valid_senders = [
        "jobs-listings@linkedin.com",
        "messages-noreply@linkedin.com",
        "jobalerts-noreply@linkedin.com",
        "notifications-noreply@linkedin.com",
        "linkedin@e.linkedin.com",
    ]
    sender_valid = any(s in sender for s in valid_senders) or sender.endswith("@linkedin.com") or sender.endswith("@e.linkedin.com")
    if not sender_valid:
        logger.warning("Email confirmation rejected: sender '%s' is not a verified LinkedIn address.", sender)
        return False, "invalid_linkedin_sender", {"sender": sender}

    # 2. Application-Confirmation Semantics
    # e.g. "Your application was sent to Xorora", "You applied to...", "Your application to..."
    subj_clean = subject.lower()
    confirmation_patterns = [
        r"your application (?:was sent|has been sent|to|for)",
        r"you applied to",
        r"application submitted",
        r"application confirmed",
        r"thanks for applying",
        r"application received",
    ]
    has_confirm_semantics = any(re.search(pat, subj_clean) for pat in confirmation_patterns)
    if not has_confirm_semantics:
        logger.warning("Email confirmation rejected: subject '%s' lacks application confirmation semantics.", subject)
        return False, "non_application_email_subject", {"subject": subject}

    # 3. Candidate / Account Association
    cand_emails = set()
    if candidate:
        if getattr(candidate, "email", None):
            cand_emails.add(candidate.email.lower().strip())
        if hasattr(candidate, "user") and candidate.user and candidate.user.email:
            cand_emails.add(candidate.user.email.lower().strip())
        app_prof = getattr(candidate, "application_profile", None)
        if app_prof and app_prof.email:
            cand_emails.add(app_prof.email.lower().strip())

    if cand_emails and recipient:
        if not any(cand_email in recipient for cand_email in cand_emails):
            logger.warning("Email confirmation rejected: recipient '%s' does not match candidate emails %s", recipient, cand_emails)
            return False, "candidate_recipient_mismatch", {"recipient": recipient}

    # 4. Job-Specific Company / Title / External ID Match
    job_company = (getattr(job, "company", "") or "").lower().strip()
    job_title = (getattr(job, "title", "") or "").lower().strip()
    ext_job_id = str(email_metadata.get("external_job_id") or getattr(job, "external_job_id", "") or "")

    company_matched = False
    if job_company:
        # Check if job company name is present in subject or email metadata
        if job_company in subj_clean or job_company in str(email_metadata.get("company", "")).lower():
            company_matched = True
        else:
            # Check normalized tokens (e.g. "Xorora Inc" -> "xorora")
            co_tokens = [t for t in re.split(r"\W+", job_company) if len(t) > 3]
            if any(tok in subj_clean for tok in co_tokens):
                company_matched = True

    title_matched = False
    if job_title:
        title_tokens = [t for t in re.split(r"\W+", job_title) if len(t) > 3 and t not in ["engineer", "developer", "senior", "lead", "staff", "full", "stack"]]
        if any(tok in subj_clean for tok in title_tokens) or job_title in subj_clean:
            title_matched = True

    id_matched = bool(ext_job_id and ext_job_id in (subj_clean + " " + str(email_metadata.get("body_snippet", ""))))

    if not (company_matched or (title_matched and id_matched) or (company_matched and title_matched)):
        logger.warning(
            "Email confirmation rejected: email subject '%s' did not match company '%s' or job title '%s'.",
            subject, job_company, job_title
        )
        return False, "job_metadata_mismatch", {
            "subject": subject,
            "expected_company": job_company,
            "expected_title": job_title
        }

    # Deterministic message hash for audit trail (zero raw body persistence)
    hash_seed = f"{message_id}:{sender}:{recipient}:{subject}:{received_at}"
    provenance_hash = hashlib.sha256(hash_seed.encode("utf-8")).hexdigest()

    evidence_meta = {
        "provider": "linkedin",
        "evidence_type": ConfirmationEvidenceType.LINKEDIN_EMAIL,
        "sender": sender,
        "subject_snippet": subject[:120],
        "recipient": recipient,
        "company": getattr(job, "company", ""),
        "job_title": getattr(job, "title", ""),
        "external_job_id": getattr(job, "external_job_id", "") or ext_job_id,
        "message_id_hash": provenance_hash,
        "received_at": str(received_at) if received_at else timezone.now().isoformat(),
        "confirmed_at": timezone.now().isoformat(),
    }

    return True, ConfirmationEvidenceType.LINKEDIN_EMAIL, evidence_meta


def can_downgrade_confirmation(
    application,
    new_verification_status: str,
    new_evidence_type: str
) -> bool:
    """
    Enforces Evidence Precedence Rule:
    If an application already has valid positive provider confirmation,
    a subsequent UNKNOWN, selector failure, or weak tracker check CANNOT downgrade it.
    Only stronger contradictory evidence from manual operator review may update it.
    """
    if not application:
        return True

    # If not previously confirmed, any valid state transition is allowed
    if not application.externally_confirmed and application.status != "submitted":
        return True

    # Application is already externally confirmed
    # Weak failure / UNKNOWN / tracker selector failure cannot downgrade
    if new_verification_status in (VerificationStatus.UNKNOWN, "unknown", "tracker_unmatched_or_unavailable", "no_session"):
        logger.info("Preserving confirmed status for App #%s: weak check (%s) cannot downgrade positive evidence.", application.id, new_evidence_type)
        return False

    if new_verification_status in (VerificationStatus.NOT_APPLIED, "not_applied") and new_evidence_type != "manual_operator_rejection":
        # Tracker empty or general selector failure cannot downgrade positive evidence
        if application.confirmation_evidence_type in STRONG_POSITIVE_EVIDENCE:
            logger.warning(
                "Preserving confirmed status for App #%s: existing '%s' evidence precedence prevents downgrade from '%s'.",
                application.id, application.confirmation_evidence_type, new_evidence_type
            )
            return False

    return True


def record_external_confirmation(
    application,
    provider: str,
    external_job_id: str,
    evidence_type: str,
    checked_at: Optional[Any] = None,
    metadata: Optional[Dict[str, Any]] = None
):
    """Controlled service method for registering verified external confirmation from a real provider."""
    if not provider or not external_job_id or not evidence_type:
        raise ValueError("Provider, external_job_id, and evidence_type are mandatory for external confirmation.")

    application.externally_confirmed = True
    application.status = "submitted"
    application.confirmation_provider = str(provider)
    application.confirmation_external_job_id = str(external_job_id)
    application.confirmation_evidence_type = str(evidence_type)
    application.confirmation_checked_at = checked_at or timezone.now()
    application.confirmation_metadata = metadata or {}
    if not application.submission_date:
        application.submission_date = checked_at or timezone.now()
    application.save(update_fields=[
        "externally_confirmed", "status", "confirmation_provider",
        "confirmation_external_job_id", "confirmation_evidence_type",
        "confirmation_checked_at", "confirmation_metadata", "submission_date",
        "updated_at"
    ])
    return application
