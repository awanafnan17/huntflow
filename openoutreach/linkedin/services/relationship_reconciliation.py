# openoutreach/linkedin/services/relationship_reconciliation.py
"""
Target-Scoped Relationship Reconciliation Service for OpenOutreach.

Provides robust, fail-closed classification of LinkedIn connection relationships.
Guarantees that:
1. Controls in sidebars, ads, recommendations ("People Also Viewed"), and footers are strictly ignored.
2. Positive 1st-degree evidence requires explicit API degree == 1 or a target-scoped 1st-degree DOM badge.
3. Action buttons (Connect, Pending, Message) are validated against the target profile top card and identity.
4. Ambiguous evidence, absence of controls, or Message-only buttons fail closed to UNKNOWN.
5. No mutating clicks (e.g. expanding 'More' menus) are performed.
"""
from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

from django.utils import timezone

logger = logging.getLogger(__name__)


class RelationshipClassification(str, enum.Enum):
    CONNECTED = "CONNECTED"
    PENDING = "PENDING"
    NOT_CONNECTED = "NOT_CONNECTED"
    UNKNOWN = "UNKNOWN"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    AUTH_REQUIRED = "AUTH_REQUIRED"


@dataclass
class RelationshipReconciliationResult:
    classification: RelationshipClassification
    evidence: str
    evidence_sufficient: bool
    observed_degree: Optional[int] = None
    target_identity: str = ""
    target_public_identifier: str = ""
    target_buttons_found: Optional[List[Dict[str, str]]] = None
    unrelated_controls_ignored: Optional[List[Dict[str, str]]] = None
    security_signal: str = "NONE"


def _extract_name_tokens(name_or_id: str) -> List[str]:
    """Extract lowercase alpha tokens of length >= 2 from name or identifier."""
    if not name_or_id:
        return []
    clean = re.sub(r"[^a-zA-Z0-9\s]", " ", name_or_id.lower())
    return [t for t in clean.split() if len(t) >= 2]


def is_aria_matching_target(aria_label: Any, target_name: str, target_public_identifier: str) -> Tuple[bool, bool]:
    """
    Evaluate if an aria-label belongs to the target or to an unrelated individual.
    
    Returns:
        (is_valid_for_target, is_explicit_unrelated_person)
    """
    if not aria_label or not isinstance(aria_label, str):
        return True, False

    aria_lower = aria_label.lower()
    target_tokens = set(_extract_name_tokens(target_name) + _extract_name_tokens(target_public_identifier))

    # Patterns indicating person-specific action labels
    # e.g., "Invite Muhammad Mashhood . to connect", "Connect with John Doe", "Withdraw invitation sent to Jane"
    invite_match = re.search(r"(?:invite|connect with|invitation sent to|message)\s+([a-zA-Z0-9\s\.\,\-]+?)(?:\s+to connect|\s+on linkedin|$)", aria_lower)
    if invite_match:
        named_entity = invite_match.group(1).strip()
        entity_tokens = set(_extract_name_tokens(named_entity))
        if entity_tokens:
            # If named entity has tokens but none overlap with target tokens, it's explicitly an unrelated person
            overlap = entity_tokens.intersection(target_tokens)
            if not overlap:
                return False, True
            return True, False

    # Check for direct token overlap if aria mentions "invite" or "connect"
    if any(k in aria_lower for k in ("invite", "connect with", "withdraw invitation to")):
        aria_tokens = set(_extract_name_tokens(aria_lower))
        # Exclude common stop words in action labels
        stop_words = {"invite", "to", "connect", "with", "withdraw", "invitation", "sent", "on", "linkedin", "more", "actions", "message", "pending"}
        distinct_name_tokens = aria_tokens - stop_words
        if distinct_name_tokens:
            overlap = distinct_name_tokens.intersection(target_tokens)
            if not overlap and len(target_tokens) > 0:
                return False, True

    return True, False


def classify_target_relationship_from_dom_data(
    target_name: str,
    target_public_identifier: str,
    api_degree: Optional[int] = None,
    top_card_degree_text: Optional[str] = None,
    top_card_buttons: Optional[List[Dict[str, str]]] = None,
    page_buttons: Optional[List[Dict[str, str]]] = None,
    security_signal: str = "NONE",
) -> RelationshipReconciliationResult:
    """
    Pure, deterministic classifier that evaluates target-scoped evidence against rules.
    """
    target_buttons = top_card_buttons or []
    all_page_buttons = page_buttons or []
    unrelated_ignored: List[Dict[str, str]] = []

    # 1. Evaluate Security Signals
    if security_signal and security_signal != "NONE":
        sig_upper = security_signal.upper()
        if sig_upper in ("CHECKPOINT", "CHALLENGE", "CAPTCHA", "RESTRICTION") or "WARNING" in sig_upper or "REDIRECT" in sig_upper:
            return RelationshipReconciliationResult(
                classification=RelationshipClassification.SAFETY_BLOCKED,
                evidence=f"Provider safety signal detected: {security_signal}",
                evidence_sufficient=True,
                observed_degree=api_degree,
                target_identity=target_name,
                target_public_identifier=target_public_identifier,
                security_signal=security_signal,
            )
        elif sig_upper in ("AUTH_REQUIRED", "LOGIN", "AUTHWALL", "SESSION_EXPIRED"):
            return RelationshipReconciliationResult(
                classification=RelationshipClassification.AUTH_REQUIRED,
                evidence=f"Provider authentication required: {security_signal}",
                evidence_sufficient=True,
                observed_degree=api_degree,
                target_identity=target_name,
                target_public_identifier=target_public_identifier,
                security_signal=security_signal,
            )

    # 2. High-confidence positive provider API evidence (degree == 1)
    if api_degree == 1:
        return RelationshipReconciliationResult(
            classification=RelationshipClassification.CONNECTED,
            evidence=f"Positive 1st-degree connection confirmed via provider API (degree=1)",
            evidence_sufficient=True,
            observed_degree=1,
            target_identity=target_name,
            target_public_identifier=target_public_identifier,
            target_buttons_found=target_buttons,
        )

    # 3. Filter top-card buttons and inspect page-wide buttons for unrelated leakage
    valid_target_buttons = []
    for b in target_buttons:
        aria = b.get("aria", "")
        is_valid, is_unrelated = is_aria_matching_target(aria, target_name, target_public_identifier)
        if is_valid and not is_unrelated:
            valid_target_buttons.append(b)
        else:
            unrelated_ignored.append(b)

    # Also log any non-top-card buttons on page that might have leaked
    for b in all_page_buttons:
        if b not in target_buttons:
            aria = b.get("aria", "")
            is_valid, is_unrelated = is_aria_matching_target(aria, target_name, target_public_identifier)
            if is_unrelated or any(k in b.get("text", "").lower() for k in ("connect", "pending", "withdraw")):
                unrelated_ignored.append(b)

    has_target_pending = any(
        "pending" in b.get("text", "").lower() or
        "pending" in b.get("aria", "").lower() or
        "withdraw" in b.get("text", "").lower() or
        "withdraw" in b.get("aria", "").lower()
        for b in valid_target_buttons
    )

    has_target_connect = any(
        "connect" in b.get("text", "").lower() or
        "invite" in b.get("aria", "").lower()
        for b in valid_target_buttons
    )

    has_target_message = any(
        "message" in b.get("text", "").lower() or
        "message" in b.get("aria", "").lower()
        for b in valid_target_buttons
    )

    # 4. Check target-scoped DOM degree badge
    has_target_1st_badge = False
    has_target_2nd_3rd_badge = False
    if top_card_degree_text:
        d_clean = top_card_degree_text.lower().strip()
        if "1st" in d_clean:
            has_target_1st_badge = True
        elif "2nd" in d_clean or "3rd" in d_clean:
            has_target_2nd_3rd_badge = True

    # 5. Non-first degree positively reported by API (2 or 3)
    if api_degree in (2, 3):
        if has_target_pending:
            return RelationshipReconciliationResult(
                classification=RelationshipClassification.PENDING,
                evidence=f"Invitation remains outstanding (target Pending control visible, api_degree={api_degree})",
                evidence_sufficient=True,
                observed_degree=api_degree,
                target_identity=target_name,
                target_public_identifier=target_public_identifier,
                target_buttons_found=valid_target_buttons,
                unrelated_controls_ignored=unrelated_ignored,
            )
        elif has_target_connect:
            return RelationshipReconciliationResult(
                classification=RelationshipClassification.NOT_CONNECTED,
                evidence=f"Target profile displays Connect action (api_degree={api_degree})",
                evidence_sufficient=True,
                observed_degree=api_degree,
                target_identity=target_name,
                target_public_identifier=target_public_identifier,
                target_buttons_found=valid_target_buttons,
                unrelated_controls_ignored=unrelated_ignored,
            )
        else:
            # Positive non-first degree provider data
            return RelationshipReconciliationResult(
                classification=RelationshipClassification.NOT_CONNECTED,
                evidence=f"Positive non-1st-degree relationship confirmed via provider API (degree={api_degree})",
                evidence_sufficient=True,
                observed_degree=api_degree,
                target_identity=target_name,
                target_public_identifier=target_public_identifier,
                target_buttons_found=valid_target_buttons,
                unrelated_controls_ignored=unrelated_ignored,
            )

    # 6. DOM-only evaluation (api_degree is None)
    if has_target_1st_badge and not has_target_pending and not has_target_connect:
        return RelationshipReconciliationResult(
            classification=RelationshipClassification.CONNECTED,
            evidence=f"Positive 1st-degree connection established via target top-card badge ('{top_card_degree_text}')",
            evidence_sufficient=True,
            observed_degree=1,
            target_identity=target_name,
            target_public_identifier=target_public_identifier,
            target_buttons_found=valid_target_buttons,
            unrelated_controls_ignored=unrelated_ignored,
        )

    if has_target_pending:
        return RelationshipReconciliationResult(
            classification=RelationshipClassification.PENDING,
            evidence=f"Target profile top card displays Pending invitation control",
            evidence_sufficient=True,
            observed_degree=api_degree,
            target_identity=target_name,
            target_public_identifier=target_public_identifier,
            target_buttons_found=valid_target_buttons,
            unrelated_controls_ignored=unrelated_ignored,
        )

    if has_target_connect:
        return RelationshipReconciliationResult(
            classification=RelationshipClassification.NOT_CONNECTED,
            evidence=f"Target profile top card displays Connect action button",
            evidence_sufficient=True,
            observed_degree=api_degree,
            target_identity=target_name,
            target_public_identifier=target_public_identifier,
            target_buttons_found=valid_target_buttons,
            unrelated_controls_ignored=unrelated_ignored,
        )

    if has_target_2nd_3rd_badge and not has_target_pending:
        return RelationshipReconciliationResult(
            classification=RelationshipClassification.NOT_CONNECTED,
            evidence=f"Target profile top card displays non-1st-degree badge ('{top_card_degree_text}')",
            evidence_sufficient=True,
            observed_degree=None,
            target_identity=target_name,
            target_public_identifier=target_public_identifier,
            target_buttons_found=valid_target_buttons,
            unrelated_controls_ignored=unrelated_ignored,
        )

    # Message button alone or absence of controls -> UNKNOWN (fail closed)
    if has_target_message:
        return RelationshipReconciliationResult(
            classification=RelationshipClassification.UNKNOWN,
            evidence=f"Message button visible on target profile without positive connection degree (InMail/Open Profile possible)",
            evidence_sufficient=False,
            observed_degree=api_degree,
            target_identity=target_name,
            target_public_identifier=target_public_identifier,
            target_buttons_found=valid_target_buttons,
            unrelated_controls_ignored=unrelated_ignored,
        )

    return RelationshipReconciliationResult(
        classification=RelationshipClassification.UNKNOWN,
        evidence=f"No target-scoped relationship controls or degree evidence available on profile page",
        evidence_sufficient=False,
        observed_degree=api_degree,
        target_identity=target_name,
        target_public_identifier=target_public_identifier,
        target_buttons_found=valid_target_buttons,
        unrelated_controls_ignored=unrelated_ignored,
    )


def extract_and_classify_page_relationship(
    page,
    target_name: str,
    target_public_identifier: str,
    api_degree: Optional[int] = None,
    security_signal: str = "NONE",
) -> RelationshipReconciliationResult:
    """
    Extracts target top-card elements from a live Playwright page and classifies relationship.
    Strictly excludes sidebar modules, right rails, 'People Also Viewed', ads, and footer controls.
    """
    top_card_degree_text = None
    top_card_buttons: List[Dict[str, str]] = []
    page_buttons: List[Dict[str, str]] = []

    try:
        # Scope strictly to the main top card container containing h1
        # Primary container: main section:has(h1) or .pv-top-card
        top_card_loc = page.locator("main section:has(h1)").first
        if top_card_loc.count() == 0:
            top_card_loc = page.locator(".pv-top-card, div.pv-top-card, main div:has(h1)").first

        if top_card_loc.count() > 0:
            # Extract degree text inside top card
            degree_loc = top_card_loc.locator(".dist-value, span.badge, .pv-top-card--list span")
            count = degree_loc.count()
            for i in range(min(count, 5)):
                txt = (degree_loc.nth(i).inner_text() or "").strip()
                if any(k in txt for k in ("1st", "2nd", "3rd")):
                    top_card_degree_text = txt
                    break

            # Extract buttons inside top card CTA action container
            btn_loc = top_card_loc.locator("button, a.artdeco-button")
            btn_count = btn_loc.count()
            for i in range(btn_count):
                b = btn_loc.nth(i)
                txt = (b.inner_text() or "").strip()
                aria = (b.get_attribute("aria-label") or "").strip()
                if txt or aria:
                    top_card_buttons.append({"text": txt, "aria": aria})

        # Collect page-wide buttons for regression & leakage auditing
        all_btn_loc = page.locator("button, a.artdeco-button")
        all_count = all_btn_loc.count()
        for i in range(min(all_count, 30)):
            b = all_btn_loc.nth(i)
            txt = (b.inner_text() or "").strip()
            aria = (b.get_attribute("aria-label") or "").strip()
            if txt or aria:
                page_buttons.append({"text": txt, "aria": aria})

    except Exception as e:
        logger.warning("DOM extraction note during relationship reconciliation: %s", e)

    return classify_target_relationship_from_dom_data(
        target_name=target_name,
        target_public_identifier=target_public_identifier,
        api_degree=api_degree,
        top_card_degree_text=top_card_degree_text,
        top_card_buttons=top_card_buttons,
        page_buttons=page_buttons,
        security_signal=security_signal,
    )


# ── Durable Relationship Persistence & Cooldown Policy ─────────────────────

UNKNOWN_FIRST_COOLDOWN_HOURS: int = 72
UNKNOWN_SECOND_COOLDOWN_DAYS: int = 7
UNKNOWN_MAX_ATTEMPTS_BEFORE_OPERATOR_REVIEW: int = 3
CONNECTED_EVIDENCE_TTL_DAYS: int = 7
PENDING_COOLDOWN_HOURS: int = 72
NOT_CONNECTED_COOLDOWN_DAYS: int = 30


def calculate_unknown_cooldown(consecutive_unknowns: int) -> Tuple[timedelta, bool]:
    """
    Progressive backoff for consecutive UNKNOWN relationship observations:
    - 1st consecutive UNKNOWN: >= 72 hours (3 days)
    - 2nd consecutive UNKNOWN: >= 7 days
    - 3rd consecutive UNKNOWN (>= 3): operator review required before another check
    """
    if consecutive_unknowns <= 1:
        return timedelta(hours=UNKNOWN_FIRST_COOLDOWN_HOURS), False
    elif consecutive_unknowns == 2:
        return timedelta(days=UNKNOWN_SECOND_COOLDOWN_DAYS), False
    else:
        return timedelta(days=14), True


def get_or_create_provider_relationship(
    workspace,
    lead,
    linkedin_profile=None,
    deal=None,
):
    """
    Retrieve or create the canonical ProviderRelationship record for an exact (workspace, profile, lead).
    Fails closed on missing or ambiguous provider account.
    """
    from openoutreach.linkedin.models import ProviderRelationship, LinkedInProfile
    from openoutreach.core.models import Workspace

    ws = workspace if isinstance(workspace, Workspace) else getattr(lead, "workspace", None)
    if ws is None and deal and hasattr(deal, "campaign"):
        ws = getattr(deal.campaign, "workspace", None)
    if ws is None:
        raise ValueError("Explicit Workspace is required for ProviderRelationship")

    prof = linkedin_profile if isinstance(linkedin_profile, LinkedInProfile) else None
    if prof is None and deal and hasattr(deal, "campaign") and getattr(deal.campaign, "linkedin_profile", None):
        prof = deal.campaign.linkedin_profile

    if prof is None:
        raise ValueError("Explicit LinkedInProfile is required for ProviderRelationship — ambiguous fallback prohibited")

    # Enforce strict tenancy
    if prof.workspace and prof.workspace_id != ws.id:
        raise ValueError(f"LinkedInProfile {prof.id} does not belong to Workspace {ws.id}")
    if getattr(lead, "workspace_id", None) and lead.workspace_id != ws.id:
        raise ValueError(f"Lead {lead.id} workspace mismatch with Workspace {ws.id}")

    rel, _ = ProviderRelationship.objects.get_or_create(
        workspace=ws,
        linkedin_profile=prof,
        lead=lead,
        defaults={
            "deal": deal,
            "classification": "UNKNOWN",
            "evidence_sufficient": False,
            "consecutive_unknown_count": 0,
        }
    )
    if deal and not rel.deal:
        rel.deal = deal
        rel.save(update_fields=["deal"])
    return rel


def record_relationship_observation(
    lead,
    result: RelationshipReconciliationResult,
    workspace=None,
    linkedin_profile=None,
    deal=None,
    ledger=None,
):
    """
    Durable updater for ProviderRelationship state following an operator or provider read.
    Enforces strict provider account scope, conservative cooldown, progressive backoff, and evidence caching.
    """
    from openoutreach.linkedin.models import ProviderRelationship, ProviderReadLedger
    from openoutreach.linkedin.services.evidence_cache import set_cached_evidence

    rel = get_or_create_provider_relationship(
        workspace=workspace,
        lead=lead,
        linkedin_profile=linkedin_profile,
        deal=deal,
    )

    if ledger and isinstance(ledger, ProviderReadLedger):
        if ledger.workspace_id != rel.workspace_id or (ledger.linkedin_profile_id and ledger.linkedin_profile_id != rel.linkedin_profile_id):
            raise ValueError(
                f"ProviderReadLedger #{ledger.id} (profile={ledger.linkedin_profile_id}, ws={ledger.workspace_id}) "
                f"mismatch with relationship target (profile={rel.linkedin_profile_id}, ws={rel.workspace_id})"
            )
        rel.last_read_ledger = ledger

    now = timezone.now()
    classification_str = result.classification.value if hasattr(result.classification, "value") else str(result.classification)
    rel.classification = classification_str
    rel.observed_degree = result.observed_degree
    rel.evidence = result.evidence
    rel.evidence_sufficient = result.evidence_sufficient
    rel.last_observed_at = now
    rel.last_safety_signal = getattr(result, "security_signal", "NONE") or "NONE"

    if classification_str == "UNKNOWN":
        rel.consecutive_unknown_count += 1
        cooldown_td, op_review = calculate_unknown_cooldown(rel.consecutive_unknown_count)
        rel.next_eligible_reconciliation_at = now + cooldown_td
        rel.operator_review_required = op_review
        if rel.deal and getattr(rel.deal, "state", None) == "Pending":
            rel.deal.next_check_pending_at = rel.next_eligible_reconciliation_at
            rel.deal.save(update_fields=["next_check_pending_at"])

    elif classification_str == "PENDING":
        rel.consecutive_unknown_count = 0
        rel.operator_review_required = False
        rel.next_eligible_reconciliation_at = now + timedelta(hours=PENDING_COOLDOWN_HOURS)
        if rel.deal and getattr(rel.deal, "state", None) == "Pending":
            rel.deal.next_check_pending_at = rel.next_eligible_reconciliation_at
            rel.deal.save(update_fields=["next_check_pending_at"])

    elif classification_str == "NOT_CONNECTED":
        rel.consecutive_unknown_count = 0
        rel.operator_review_required = False
        rel.next_eligible_reconciliation_at = now + timedelta(days=NOT_CONNECTED_COOLDOWN_DAYS)

    elif classification_str == "CONNECTED":
        rel.consecutive_unknown_count = 0
        rel.operator_review_required = False
        rel.next_eligible_reconciliation_at = None
        if getattr(lead, "public_identifier", None):
            set_cached_evidence(
                resource_type="connection_state",
                resource_identifier=lead.public_identifier,
                data={
                    "status": "CONNECTED",
                    "degree": 1,
                    "evidence": result.evidence,
                    "observed_at": now.isoformat(),
                },
                ttl=timedelta(days=CONNECTED_EVIDENCE_TTL_DAYS),
                linkedin_profile=rel.linkedin_profile,
            )

    elif classification_str in ("SAFETY_BLOCKED", "AUTH_REQUIRED"):
        rel.operator_review_required = True
        rel.next_eligible_reconciliation_at = None

    rel.save()
    return rel


def evaluate_relationship_for_first_message(
    lead=None,
    deal=None,
    workspace=None,
    linkedin_profile=None,
) -> Tuple[bool, str]:
    """
    Evaluates whether target prospect relationship satisfies FIRST_MESSAGE requirements
    for the EXACT sending provider account:
    1. Positive CONNECTED classification for that exact provider account
    2. evidence_sufficient is True
    3. Fresh within CONNECTED_EVIDENCE_TTL_DAYS (7 days)
    4. Deal state is Connected
    
    Returns (is_allowed, reason).
    """
    target_lead = lead or (deal.lead if deal else None)
    if not target_lead:
        return False, "missing_lead_context"

    if deal and getattr(deal, "state", None) != "Connected":
        return False, "deal_not_connected_cannot_first_message"

    from openoutreach.linkedin.models import LinkedInProfile, ProviderRelationship
    from openoutreach.core.models import Workspace

    ws = workspace if isinstance(workspace, Workspace) else getattr(target_lead, "workspace", None)
    if ws is None and deal and hasattr(deal, "campaign"):
        ws = getattr(deal.campaign, "workspace", None)

    prof = linkedin_profile if isinstance(linkedin_profile, LinkedInProfile) else None
    if prof is None and deal and hasattr(deal, "campaign") and getattr(deal.campaign, "linkedin_profile", None):
        prof = deal.campaign.linkedin_profile

    if prof is None:
        return False, "missing_provider_relationship_context"

    if ws is None:
        ws = prof.workspace

    rel = ProviderRelationship.objects.filter(
        workspace=ws,
        linkedin_profile=prof,
        lead=target_lead,
    ).first()
    if not rel:
        return False, "no_provider_relationship_evidence"

    if rel.classification != "CONNECTED":
        return False, f"provider_relationship_{rel.classification.lower()}_cannot_first_message"

    if not rel.evidence_sufficient:
        return False, "insufficient_relationship_evidence"

    if not rel.is_connection_fresh(max_age_days=CONNECTED_EVIDENCE_TTL_DAYS):
        return False, "stale_relationship_evidence"

    return True, "OK"


def reconcile_target_relationship(
    workspace_id: int,
    linkedin_profile_id: int,
    lead_id: Optional[int] = None,
    deal_id: Optional[int] = None,
    operator_approval=None,
    force: bool = False,
    session=None,
) -> Dict[str, Any]:
    """
    Generic canonical entry point for future single relationship reconciliation.
    Strictly tenant-isolated, account-isolated, rate-budgeted, safety-checked,
    and deduplicated/coalesced.

    NOTE: This function does NOT execute automated provider network requests unless
    invoked with an explicit authenticated session and passes all safety and cooldown gates.
    """
    from openoutreach.core.models import Workspace
    from openoutreach.crm.models import Lead, Deal
    from openoutreach.linkedin.models import (
        LinkedInProfile,
        ProviderRelationship,
        ProviderReadLedger,
        ProviderOperationType,
    )
    from openoutreach.linkedin.services.account_governor import (
        is_account_safe_for_provider_read,
    )
    from openoutreach.linkedin.services.evidence_cache import (
        get_cached_evidence,
        record_provider_read,
    )

    # 1. Tenant & Profile Isolation
    ws = Workspace.objects.filter(id=workspace_id, status="active").first()
    if not ws:
        return {"success": False, "status": "blocked", "error": "invalid_or_inactive_workspace"}

    profile = LinkedInProfile.objects.filter(id=linkedin_profile_id, active=True).first()
    if not profile or (profile.workspace and profile.workspace.id != ws.id):
        return {"success": False, "status": "blocked", "error": "unauthorized_or_inactive_provider_profile"}

    # 2. Target Resolution & Isolation
    lead = None
    deal = None
    if deal_id:
        deal = Deal.objects.filter(id=deal_id).select_related("lead", "campaign").first()
        if not deal or getattr(deal.campaign, "workspace_id", None) != ws.id:
            return {"success": False, "status": "blocked", "error": "deal_workspace_mismatch"}
        lead = deal.lead
    elif lead_id:
        lead = Lead.objects.filter(id=lead_id).first()
        if not lead or (lead.workspace and lead.workspace.id != ws.id):
            return {"success": False, "status": "blocked", "error": "lead_workspace_mismatch"}

    if not lead or not lead.public_identifier:
        return {"success": False, "status": "blocked", "error": "missing_or_invalid_target_lead"}

    # 3. Check Existing Relationship Cooldown & Operator Review
    rel = ProviderRelationship.objects.filter(workspace=ws, linkedin_profile=profile, lead=lead).first()
    now = timezone.now()
    if rel and not force:
        if rel.operator_review_required and not operator_approval:
            return {
                "success": False,
                "status": "blocked",
                "reason": "operator_review_required",
                "relationship": rel.classification,
                "consecutive_unknowns": rel.consecutive_unknown_count,
            }
        if rel.next_eligible_reconciliation_at and now < rel.next_eligible_reconciliation_at:
            return {
                "success": False,
                "status": "cooldown",
                "reason": "relationship_cooldown_active",
                "relationship": rel.classification,
                "next_eligible_at": rel.next_eligible_reconciliation_at.isoformat(),
            }

    # 4. Provider Safety & Session Health Checks
    if profile.session_status != "valid":
        return {
            "success": False,
            "status": "blocked",
            "reason": f"provider_session_invalid ({profile.session_status})",
        }

    is_safe, safety_reason = is_account_safe_for_provider_read(workspace=ws, linkedin_profile=profile)
    if not is_safe:
        return {
            "success": False,
            "status": "blocked",
            "reason": f"provider_safety_blocked ({safety_reason})",
        }

    # 5. Cached Evidence Lookup (Coalescing)
    if not force:
        cached = get_cached_evidence(
            resource_type="connection_state",
            resource_identifier=lead.public_identifier,
            linkedin_profile=profile,
        )
        if cached is not None:
            record_provider_read(
                operation_type=ProviderOperationType.PROFILE_DETAIL,
                resource_identifier=lead.public_identifier,
                cache_hit=True,
                linkedin_profile=profile,
                workspace=ws,
                success=True,
                read_cost=0,
                metadata={"purpose": "relationship_reconciliation", "coalesced": True},
            )
            return {
                "success": True,
                "status": "cached",
                "cached": True,
                "classification": cached.get("status", "UNKNOWN"),
                "data": cached,
            }

    # 6. Guard against unauthorized headless execution without explicit session
    if session is None:
        return {
            "success": False,
            "status": "standby",
            "reason": "no_active_session_provided",
            "message": "Reconciliation requires an explicit live browser session provided by operator or worker.",
        }

    # 7. Live Extraction & Classification (only with explicit session)
    page = getattr(session, "page", None)
    if not page:
        return {"success": False, "status": "error", "error": "session_has_no_active_page"}

    classification_result = extract_and_classify_page_relationship(
        page=page,
        target_name=f"{lead.first_name} {lead.last_name}".strip(),
        target_public_identifier=lead.public_identifier,
    )

    ledger_entry = record_provider_read(
        operation_type=ProviderOperationType.PROFILE_DETAIL,
        resource_identifier=lead.public_identifier,
        cache_hit=False,
        linkedin_profile=profile,
        workspace=ws,
        success=True,
        read_cost=1,
        metadata={
            "purpose": "relationship_reconciliation",
            "target": lead.public_identifier,
            "classification": classification_result.classification.value,
            "evidence": classification_result.evidence,
            "observed_degree": classification_result.observed_degree,
        },
    )

    updated_rel = record_relationship_observation(
        lead=lead,
        result=classification_result,
        workspace=ws,
        linkedin_profile=profile,
        deal=deal,
        ledger=ledger_entry,
    )

    return {
        "success": True,
        "status": "reconciled",
        "cached": False,
        "classification": updated_rel.classification,
        "evidence_sufficient": updated_rel.evidence_sufficient,
        "evidence": updated_rel.evidence,
        "next_eligible_at": updated_rel.next_eligible_reconciliation_at.isoformat() if updated_rel.next_eligible_reconciliation_at else None,
    }

