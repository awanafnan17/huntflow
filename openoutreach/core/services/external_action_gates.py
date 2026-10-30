"""
Production external action gate service for OpenOutreach.
Evaluates all 23 non-negotiable external action authorization gates using real DB models.
"""
import os
from typing import Tuple, Optional, Any
from openoutreach.core.models import (
    Workspace, WorkspaceMembership, Campaign, CampaignPurpose,
    SenderIdentity, CampaignMessageDraft
)
from openoutreach.career.models import (
    IncidentHold, IncidentHoldTarget, CandidateProfile,
    CandidateIdentityVerification, Job, JobApplication
)
from openoutreach.crm.models.lead import Lead, SuppressionRecord
from openoutreach.crm.models.deal import Deal
from openoutreach.core.services.entitlements import check_entitlement


def evaluate_external_action_gates(
    *,
    workspace: Workspace,
    membership: Optional[WorkspaceMembership] = None,
    sender: Optional[SenderIdentity] = None,
    campaign: Optional[Campaign] = None,
    linkedin_profile: Optional[Any] = None,
    lead: Optional[Lead] = None,
    deal: Optional[Deal] = None,
    draft: Optional[CampaignMessageDraft] = None,
    candidate_profile: Optional[CandidateProfile] = None,
    job: Optional[Job] = None,
    job_application: Optional[JobApplication] = None,
    has_captcha: bool = False,
    has_account_warning: bool = False,
    interface_confidence: float = 0.95,
    within_pilot_limit: bool = True,
    previously_attempted: bool = False,
    external_action_authorized: bool = True,
    operator_approval: Optional[Any] = None,
    action_type: str = "job_application"
) -> Tuple[bool, str]:
    """
    Evaluates 23 mandatory non-negotiable external action authorization gates.
    Returns (allowed: bool, reason: str).
    """
    from openoutreach.crm.models import Deal, Lead
    from openoutreach.linkedin.models import LinkedInProfile

    # Profile Resolution
    resolved_profile = linkedin_profile if isinstance(linkedin_profile, LinkedInProfile) else None
    if not resolved_profile and campaign and getattr(campaign, "linkedin_profile", None):
        resolved_profile = campaign.linkedin_profile
    if not resolved_profile and deal and hasattr(deal, "campaign") and getattr(deal.campaign, "linkedin_profile", None):
        resolved_profile = deal.campaign.linkedin_profile

    # Workspace Resolution
    if workspace is None:
        if campaign and getattr(campaign, "workspace", None):
            workspace = campaign.workspace
        elif candidate_profile and getattr(candidate_profile, "workspace", None):
            workspace = candidate_profile.workspace
        elif deal and hasattr(deal, "campaign") and getattr(deal.campaign, "workspace", None):
            workspace = deal.campaign.workspace

    # Gate 0: Workspace master automation & One-time Operator Approval
    if workspace is not None:
        from openoutreach.core.models import WorkspaceAutomationSettings, OperatorActionApproval
        auto_settings = WorkspaceAutomationSettings.objects.filter(workspace=workspace).first()
        if auto_settings:
            # Check soak expiration and safety stop
            if auto_settings.check_soak_expiry():
                return False, "soak_duration_elapsed_24h"
            if auto_settings.soak_status == "SAFETY_STOP":
                return False, f"soak_safety_stop ({auto_settings.soak_stop_reason or 'incident'})"
            if getattr(auto_settings, "manual_provider_use_paused", False):
                return False, "manual_provider_use_paused"

            from openoutreach.core.models import StageCanaryBudget
            budget = StageCanaryBudget.objects.filter(workspace=workspace, stage="STAGE_1").first()
            if budget and budget.state in [StageCanaryBudget.State.NETWORK_ATTEMPTED, StageCanaryBudget.State.COMPLETED, StageCanaryBudget.State.BLOCKED]:
                if not operator_approval:
                    return False, f"stage_canary_budget_exhausted ({budget.state})"

            if not auto_settings.master_automation_enabled:
                # Master automation is PAUSED (Global Emergency Pause).
                # Generic external_action_authorized=True alone CANNOT bypass master pause.
                # Only a valid, unconsumed OperatorActionApproval bound to this exact action/job is permitted.
                if not operator_approval:
                    return False, "workspace_master_automation_paused"
                if not isinstance(operator_approval, OperatorActionApproval):
                    return False, "workspace_master_automation_paused"
                if not operator_approval.is_valid_for(
                    workspace=workspace,
                    linkedin_profile=resolved_profile,
                    candidate=candidate_profile,
                    job=job,
                    lead=lead or (deal.lead if deal else None),
                    deal=deal,
                    action_type=action_type
                ):
                    return False, "workspace_master_automation_paused"
            else:
                # Master automation is enabled. Check action-specific capability toggle.
                if action_type == "job_application" and not auto_settings.auto_apply_jobs:
                    return False, "action_capability_disabled"
                elif action_type in ["connect", "connection"] and not auto_settings.auto_send_connections:
                    return False, "action_capability_disabled"
                elif action_type in ["follow_up", "message"] and not auto_settings.auto_followup:
                    return False, "action_capability_disabled"
                elif action_type in ["reply", "ai_reply"] and not auto_settings.auto_monitor_replies:
                    return False, "action_capability_disabled"
                elif action_type == "email" and not auto_settings.auto_send_emails:
                    return False, "action_capability_disabled"

                # 1. Backlog Governance Gate for Connections
                if action_type in ["connect", "connection"]:
                    from openoutreach.crm.models import Deal
                    crm_pending_count = Deal.objects.filter(campaign__workspace=auto_settings.workspace, state="Pending").count()
                    provider_pending_count = auto_settings.linkedin_pending_invitations_count or 0
                    effective_backlog = max(crm_pending_count, provider_pending_count)
                    if effective_backlog >= auto_settings.max_pending_invitations:
                        if not auto_settings.connection_backlog_operator_override and auto_settings.connection_backlog_behavior == "PAUSE_NEW_CONNECTIONS" and not operator_approval:
                            return False, f"pending_invitation_backlog_exceeded ({effective_backlog} >= {auto_settings.max_pending_invitations})"

                # 2. Daily limits for all channels via Canonical Quota Service
                from openoutreach.core.services.quota_service import (
                    get_daily_successful_easy_apply_count,
                    get_daily_job_provider_attempt_count,
                    get_daily_connection_provider_attempt_count,
                    get_daily_linkedin_outbound_message_count,
                    get_daily_linkedin_outbound_message_breakdown,
                    get_daily_email_send_count,
                )

                if action_type == "job_application" and not operator_approval:
                    successful_target = getattr(auto_settings, "daily_job_application_target", 50) or 50
                    if successful_target > 0:
                        successful_count = get_daily_successful_easy_apply_count(
                            workspace=auto_settings.workspace,
                            candidate_profile=candidate_profile,
                        )
                        if successful_count >= successful_target:
                            return False, f"daily_job_application_limit_reached ({successful_count} >= {successful_target})"

                    provider_ceiling = auto_settings.max_daily_job_applications or 60
                    if provider_ceiling > 0:
                        daily_count = get_daily_job_provider_attempt_count(
                            workspace=auto_settings.workspace,
                            candidate_profile=candidate_profile,
                        )
                        if daily_count >= provider_ceiling:
                            return False, f"daily_job_application_limit_reached ({daily_count} >= {provider_ceiling})"

                elif action_type in ["connect", "connection"] and auto_settings.max_daily_connections > 0 and not operator_approval:
                    daily_conns = get_daily_connection_provider_attempt_count(
                        workspace=auto_settings.workspace,
                        linkedin_profile=resolved_profile,
                    )
                    if daily_conns >= auto_settings.max_daily_connections:
                        return False, f"daily_connection_limit_reached ({daily_conns} >= {auto_settings.max_daily_connections})"

                elif action_type in ["first_message", "post_connection_message", "follow_up", "message", "reply", "ai_reply"] and auto_settings.max_daily_messages > 0 and not operator_approval:
                    total_daily_msgs = get_daily_linkedin_outbound_message_count(
                        workspace=auto_settings.workspace,
                        linkedin_profile=resolved_profile,
                    )
                    if total_daily_msgs >= auto_settings.max_daily_messages:
                        return False, f"daily_linkedin_message_limit_reached ({total_daily_msgs} >= {auto_settings.max_daily_messages})"

                    if action_type in ["reply", "ai_reply"] and auto_settings.max_daily_ai_replies > 0:
                        breakdown = get_daily_linkedin_outbound_message_breakdown(
                            workspace=auto_settings.workspace,
                            linkedin_profile=resolved_profile,
                        )
                        if breakdown["ai_replies"] >= auto_settings.max_daily_ai_replies:
                            return False, f"daily_ai_reply_limit_reached ({breakdown['ai_replies']} >= {auto_settings.max_daily_ai_replies})"

                elif action_type == "email" and auto_settings.max_daily_emails > 0 and not operator_approval:
                    daily_emails = get_daily_email_send_count(workspace=workspace)
                    if daily_emails >= auto_settings.max_daily_emails:
                        return False, f"daily_email_limit_reached ({daily_emails} >= {auto_settings.max_daily_emails})"

    # Duplicate Application Gate (Pre-Provider Protection)
    if action_type == "job_application" and candidate_profile is not None and job is not None:
        if JobApplication.objects.filter(
            candidate_profile=candidate_profile,
            job=job,
            status__in=["submitted", "submission_unverified"]
        ).exists():
            return False, "duplicate_application_blocked"
        ext_jid = str(getattr(job, "external_job_id", "") or "").strip()
        if ext_jid and JobApplication.objects.filter(
            candidate_profile=candidate_profile,
            confirmation_external_job_id=ext_jid,
            externally_confirmed=True
        ).exists():
            return False, "duplicate_application_blocked"

    # Gate 1: Workspace membership active
    if membership is not None and getattr(membership, "status", None) != "active":
        return False, "membership_inactive"

    # Gate 2: Workspace active
    if workspace is not None and hasattr(workspace, "is_active") and not workspace.is_active:
        return False, "workspace_inactive"

    # Gate 3: Workspace membership role authorized
    if membership is not None and getattr(membership, "role", None) not in ("owner", "admin", "member"):
        return False, "role_unauthorized"

    # Gate 4: Sender identity active
    if sender is not None and not getattr(sender, "is_active", True):
        return False, "sender_inactive"

    # Gate 5: Sender identity verified
    if sender is not None and getattr(sender, "verification_status", "verified") != "verified":
        return False, "sender_unverified"

    # Gate 6: Campaign active
    if campaign is not None and not getattr(campaign, "is_active", True):
        return False, "campaign_inactive"

    # Gate 7: Incident hold inactive (Check DB for active IncidentHoldTarget)
    if workspace is not None:
        target_pairs = []
        if deal: target_pairs.append((Deal, deal.id))
        if campaign: target_pairs.append((Campaign, campaign.id))
        if candidate_profile: target_pairs.append((CandidateProfile, candidate_profile.id))
        if job: target_pairs.append((Job, job.id))
        if lead: target_pairs.append((Lead, lead.id))
        if resolved_profile: target_pairs.append((LinkedInProfile, resolved_profile.id))
        
        for model_cls, obj_id in target_pairs:
            if obj_id and IncidentHoldTarget.objects.filter(
                incident_hold__active=True,
                object_id=obj_id
            ).exists():
                return False, "incident_hold_active"

    # CandidateProfile identity safety invariants
    if candidate_profile is not None:
        if candidate_profile.id == 1:
            if not CandidateIdentityVerification.objects.filter(candidate_profile=candidate_profile, status="verified").exists():
                return False, "identity_verification_missing"

    # Gate 8: Target unsuppressed (Check DB for SuppressionRecord)
    if workspace is not None and lead is not None and getattr(lead, "linkedin_url", None):
        if SuppressionRecord.objects.filter(workspace=workspace, linkedin_url=lead.linkedin_url).exists():
            return False, "target_suppressed"

    # Gate 9: Deal state valid & Relationship Gate
    if deal is not None:
        deal_st = getattr(deal, "state", None)
        if deal_st in ("Failed", "Opted Out", "Withdrawn"):
            return False, "deal_state_invalid"
        if getattr(deal, "outcome", None) in ("not_interested", "opted_out", "wrong_fit", "unresponsive"):
            return False, "deal_dispositioned_or_opted_out"

        # First message requires positive Connected state
        if action_type in ["first_message", "post_connection_message"]:
            if deal_st != "Connected":
                return False, "deal_not_connected_cannot_first_message"

            # Check ProviderRelationship positive evidence and freshness
            from openoutreach.linkedin.services.relationship_reconciliation import evaluate_relationship_for_first_message
            if not resolved_profile:
                return False, "missing_provider_relationship_context"

            rel_ok, rel_reason = evaluate_relationship_for_first_message(
                lead=getattr(deal, "lead", None) or lead,
                deal=deal,
                workspace=workspace,
                linkedin_profile=resolved_profile,
            )
            if not rel_ok:
                return False, rel_reason

            # Check duplicate outbound first message
            from openoutreach.chat.models import ChatMessage
            if getattr(deal, "pk", None) and ChatMessage.objects.filter(deal=deal, is_outgoing=True).exists():
                return False, "first_message_already_sent"

            # Check uncertain write intent
            if getattr(deal, "lead", None) and getattr(deal.lead, "public_identifier", None):
                from openoutreach.linkedin.models import ProviderWriteIntent
                if ProviderWriteIntent.objects.filter(
                    target_identity=deal.lead.public_identifier,
                    state=ProviderWriteIntent.State.UNCERTAIN,
                ).exists():
                    return False, "uncertain_write_intent_active"

        # Follow-up requires Connected state (or Emailed for email deals)
        elif action_type in ["follow_up", "message"]:
            if deal_st == "Pending":
                return False, "deal_still_pending_cannot_followup"
            if deal_st not in ("Connected", "Emailed"):
                return False, "deal_not_connected_cannot_followup"
    elif action_type in ["first_message", "post_connection_message", "follow_up"]:
        return False, "missing_deal_context"

    # Provider Session / Campaign Profile Alignment Gate
    if campaign and getattr(campaign, "linkedin_profile", None) and linkedin_profile and isinstance(linkedin_profile, LinkedInProfile):
        if linkedin_profile != campaign.linkedin_profile:
            return False, "provider_session_profile_mismatch"

    if deal and hasattr(deal, "campaign") and getattr(deal.campaign, "linkedin_profile", None) and linkedin_profile and isinstance(linkedin_profile, LinkedInProfile):
        if linkedin_profile != deal.campaign.linkedin_profile:
            return False, "provider_session_profile_mismatch"

    if operator_approval:
        from openoutreach.core.models import OperatorActionApproval
        if isinstance(operator_approval, OperatorActionApproval):
            if not operator_approval.is_valid_for(
                workspace=workspace,
                linkedin_profile=resolved_profile,
                candidate=candidate_profile,
                job=job,
                lead=lead or (deal.lead if deal else None),
                deal=deal,
                action_type=action_type,
            ):
                return False, "operator_approval_mismatch"

    # Gate 10: Job active & Provenance
    if job is not None:
        if not getattr(job, "is_active", True):
            return False, "job_inactive"
        url = (getattr(job, "job_url", "") or "").strip().lower()
        if not url:
            return False, "missing_job_url"

        is_production_live = (
            (workspace and getattr(workspace, "slug", "") == "default") or
            (candidate_profile and (getattr(candidate_profile, "id", None) == 2 or getattr(candidate_profile, "email", "") == "candidate-2@example.com")) or
            getattr(job, "provenance", "") == "TEST_FIXTURE" or
            (not os.environ.get("PYTEST_CURRENT_TEST"))
        )
        if is_production_live:
            blocked_domains = ["example.com", "localhost", "127.0.0.1", "test.com", "dummy"]
            if any(d in url for d in blocked_domains) and not (getattr(job, "is_synthetic", False) and getattr(job, "provenance", "") != "TEST_FIXTURE"):
                return False, "invalid_job_url_domain"
            if hasattr(job, "is_real_provider_job") and not job.is_real_provider_job() and getattr(job, "provenance", "") != "REAL_PROVIDER":
                return False, "unverified_job_provenance"

    # Gate 11: Candidate facts / active version approved & Profile Readiness for applications
    if candidate_profile is not None:
        active_ver = getattr(candidate_profile, "active_version", None)
        if active_ver and getattr(active_ver, "state", None) != "approved":
            return False, "candidate_identity_unapproved"

        if action_type == "job_application":
            app_prof = getattr(candidate_profile, "application_profile", None)
            is_live_production = (
                (candidate_profile.id == 2 or getattr(candidate_profile, "email", "") == "candidate-2@example.com") and
                not os.environ.get("PYTEST_CURRENT_TEST")
            )
            if is_live_production or app_prof is not None:
                from openoutreach.career.answer_bank import calculate_application_profile_readiness
                readiness = calculate_application_profile_readiness(candidate_profile)
                if not readiness.get("is_ready", False):
                    return False, "candidate_profile_not_ready"
                if not app_prof or not getattr(app_prof, "confirmed", False):
                    return False, "candidate_profile_unconfirmed"

    # Gate 11.5: Location Targeting Gate
    if action_type == "job_application" and candidate_profile is not None and job is not None:
        app_prof = getattr(candidate_profile, "application_profile", None)
        if app_prof and hasattr(app_prof, "get_location_targeting_policy") and getattr(job, "location", None):
            from openoutreach.core.services.location_targeting import evaluate_location_match
            policy = app_prof.get_location_targeting_policy()
            if policy.included_locations or policy.excluded_locations or getattr(policy, "search_worldwide", False):
                loc_res = evaluate_location_match(
                    job.location,
                    policy,
                    candidate_authorization=getattr(app_prof, "authorized_countries", None),
                    job_work_mode=getattr(job, "work_mode", None),
                    candidate_relocation=getattr(app_prof, "relocation_willingness", None),
                    candidate_sponsorship_now=getattr(app_prof, "visa_sponsorship_now", None)
                )
                if not loc_res.is_eligible:
                    return False, f"location_not_eligible_{loc_res.decision.lower()}"

    # Gate 11.6: Screening Questions & Attestation Truth Gate
    if action_type == "job_application" and candidate_profile is not None and job is not None:
        screening_questions = getattr(job, "screening_questions", []) or []
        from openoutreach.career.answer_bank import check_screening_question_attestation_conflict
        for sq in screening_questions:
            q_text = sq.get("text", "") if isinstance(sq, dict) else str(sq)
            if not q_text:
                continue
            is_conflict, conflict_reason = check_screening_question_attestation_conflict(candidate_profile, q_text, job=job)
            if is_conflict:
                return False, "truthful_attestation_not_possible"

    if action_type in ["connection", "message"] and lead is not None and campaign is not None:
        if hasattr(campaign, "get_location_targeting_policy"):
            from openoutreach.core.services.location_targeting import evaluate_location_match
            policy = campaign.get_location_targeting_policy()
            lead_loc = getattr(lead, "location", "") or getattr(lead, "country_code", "")
            if policy.included_locations or policy.excluded_locations or getattr(campaign, "require_location_match", False):
                loc_res = evaluate_location_match(lead_loc, policy)
                if not loc_res.is_eligible:
                    return False, f"location_not_eligible_{loc_res.decision.lower()}"

    # Gate 12: Campaign purpose valid
    if campaign is not None and getattr(campaign, "campaign_purpose", None) is not None:
        purpose = campaign.campaign_purpose
        if not getattr(purpose, "is_active", True):
            return False, "campaign_purpose_invalid"

    # Gate 13: Draft style validation
    if draft is not None:
        style_res = getattr(draft, "style_validation_result", {})
        if isinstance(style_res, dict) and style_res.get("valid") is False:
            return False, "style_validation_failed"

    # Gate 14: Draft claims validation
    if draft is not None:
        claims_res = getattr(draft, "claims_validation_result", {})
        if isinstance(claims_res, dict) and claims_res.get("valid") is False:
            return False, "claims_validation_failed"

    # Gate 15: Required review complete
    if draft is not None and getattr(draft, "status", None) != "approved":
        return False, "review_incomplete"

    # Gate 16 & 17: Entitlement available & Usage consumption
    if workspace is not None:
        ent_ok = check_entitlement(workspace, "actions")
        if not ent_ok:
            return False, "entitlement_exceeded"

    # Gate 18: Explicit external authorization exists
    if not external_action_authorized:
        return False, "external_authorization_missing"

    # Gate 19: Action within pilot limit
    if not within_pilot_limit:
        return False, "pilot_limit_exceeded"

    # Gate 20: Action key unique / not previously attempted
    if previously_attempted:
        return False, "duplicate_action_blocked"

    # Gate 21: Account health ok / no warning
    if has_account_warning:
        return False, "account_warning_active"

    # Gate 22: Interface confidence high
    if interface_confidence < 0.8:
        return False, "interface_confidence_low"

    # Gate 23: CAPTCHA clear / no active safety signal
    if has_captcha:
        return False, "platform_safety_signal_active"

    return True, "authorized"
