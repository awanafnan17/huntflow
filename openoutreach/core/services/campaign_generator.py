# openoutreach/core/services/campaign_generator.py
import hashlib
from typing import Dict, Any, Tuple, Optional
from django.core.exceptions import ValidationError
from django.utils import timezone

from openoutreach.core.models import (
    Campaign,
    MessageSequenceStep,
    CampaignMessageDraft,
    PromptPack,
    PromptPackVersion,
    CampaignPurposeVersion,
    SenderIdentity,
)
from openoutreach.core.schema_validation import validate_campaign_context_schema
from openoutreach.core.content_style import (
    validate_user_facing_content_style,
    validate_generated_claims,
)
from openoutreach.core.services.audit_service import record_audit_event
from openoutreach.crm.models.deal import Deal
from django.db import transaction


@transaction.atomic
def generate_draft_for_deal_step(
    deal: Deal,
    sequence_step: MessageSequenceStep,
    context_data: Dict[str, Any],
    user=None,
    is_synthetic: bool = False,
    synthetic_run=None,
) -> CampaignMessageDraft:
    """
    Generic SaaS Campaign Draft Generation Pipeline.
    Driven by CampaignPurposeVersion and PromptPackVersion.
    Strictly enforces draft_mode=True, sendable=False, external_action_authorized=False.
    """
    # Lock the deal row to prevent concurrent draft creation race conditions
    deal = Deal.objects.select_for_update().get(id=deal.id)
    campaign = deal.campaign
    lead = deal.lead
    workspace = campaign.workspace

    action_key = f"draft_gen_{deal.id}_{sequence_step.id}_{hashlib.md5(str(context_data).encode()).hexdigest()[:12]}"
    record_audit_event(
        workspace=workspace,
        event_type="draft_generation_started",
        actor=user,
        target=deal,
        action_key=f"start_{action_key}",
        metadata={"step_id": sequence_step.id},
    )

    # 1. Tenant & workspace boundary checks
    if workspace != lead.workspace:
        raise ValidationError("Deal lead workspace does not match campaign workspace.")
    if sequence_step.campaign != campaign:
        raise ValidationError("MessageSequenceStep does not belong to the deal campaign.")

    purpose = campaign.campaign_purpose
    if not purpose:
        raise ValidationError("Campaign must have a valid CampaignPurpose assigned.")

    # 2. Sender Identity Validation
    sender = campaign.sender_identity
    if sender and sender.workspace != workspace:
        raise ValidationError("Campaign sender identity workspace must match campaign workspace.")

    # 3. Context Schema Validation
    if purpose.context_schema:
        ok, errors = validate_campaign_context_schema(context_data, purpose.context_schema)
        if not ok:
            raise ValidationError(f"Context validation failed: {'; '.join(errors)}")

    # 4. Load Prompt Pack Version & Purpose Version
    prompt_version = sequence_step.prompt_version
    if not prompt_version:
        if purpose.prompt_packs.exists():
            pack = purpose.prompt_packs.first()
            prompt_version = pack.versions.first()
        if not prompt_version:
            pack, _ = PromptPack.objects.get_or_create(
                campaign_purpose=purpose,
                defaults={"name": f"{purpose.name} Pack", "version": 1},
            )
            prompt_version, _ = PromptPackVersion.objects.get_or_create(
                prompt_pack=pack,
                version_number=1,
                defaults={
                    "prompt_template": "Hi {recipient_name}, connecting regarding {company_name}.",
                },
            )

    purpose_version = purpose.purpose_versions.first()
    if not purpose_version:
        purpose_version, _ = CampaignPurposeVersion.objects.get_or_create(
            campaign_purpose=purpose,
            version_number=1,
            defaults={
                "context_schema": purpose.context_schema or {},
                "writing_policy_version": "v1",
                "claims_policy_version": "v1",
            },
        )

    # 5. Render template
    raw_template = prompt_version.prompt_template if prompt_version else "Hi {recipient_name}, connecting regarding {company_name}."
    rendered_text = raw_template
    for key, val in context_data.items():
        if isinstance(val, str):
            rendered_text = rendered_text.replace("{" + key + "}", val)

    checksum = hashlib.sha256(rendered_text.encode("utf-8")).hexdigest()

    # 6. Check existing draft first for idempotency
    existing_draft = CampaignMessageDraft.objects.filter(
        deal=deal,
        sequence_step=sequence_step,
        content_checksum=checksum,
    ).first()
    if existing_draft:
        return existing_draft

    # 7. Style Validation
    style_res = validate_user_facing_content_style(rendered_text, sequence_step.step_type)
    if not style_res["valid"]:
        clean_text = rendered_text.replace("--", "-").replace("—", "-").replace("–", "-")
        style_res = validate_user_facing_content_style(clean_text, sequence_step.step_type)
        if style_res["valid"]:
            rendered_text = clean_text

    record_audit_event(
        workspace=workspace,
        event_type="draft_style_validated",
        actor=user,
        target=deal,
        action_key=f"style_val_{deal.id}_{sequence_step.id}",
        metadata={"valid": style_res["valid"]},
    )

    # 8. Factual Claims Validation
    verified_facts = context_data.get("verified_candidate_facts") or context_data.get("verified_portfolio_facts") or []
    claims_res = validate_generated_claims(rendered_text, verified_facts)

    record_audit_event(
        workspace=workspace,
        event_type="draft_claims_validated",
        actor=user,
        target=deal,
        action_key=f"claims_val_{deal.id}_{sequence_step.id}",
        metadata={"valid": claims_res["valid"]},
    )

    existing_count = CampaignMessageDraft.objects.filter(
        deal=deal,
        sequence_step=sequence_step,
    ).count()
    event_type = "draft_regenerated" if existing_count > 0 else "draft_generated"

    # 9. Persist Non-Sendable Draft
    existing_draft = CampaignMessageDraft.objects.filter(
        deal=deal,
        sequence_step=sequence_step,
        content_checksum=checksum,
    ).first()
    if existing_draft:
        return existing_draft

    draft = CampaignMessageDraft.objects.create(
        workspace=workspace,
        campaign=campaign,
        deal=deal,
        lead=lead,
        sender_identity=sender,
        sequence_step=sequence_step,
        purpose_version=purpose_version,
        prompt_version=prompt_version,
        content=rendered_text,
        content_checksum=checksum,
        style_validation_result=style_res,
        claims_validation_result=claims_res,
        context_snapshot=context_data,
        sender_fact_snapshot=sender.verified_facts if sender else [],
        status="draft",
        draft_mode=True,
        sendable=False,
        external_action_authorized=False,
        is_synthetic=is_synthetic,
        synthetic_run=synthetic_run,
    )

    record_audit_event(
        workspace=workspace,
        event_type=event_type,
        actor=user,
        target=draft,
        action_key=f"draft_gen_{deal.id}_{sequence_step.id}",
        metadata={"draft_id": draft.id, "checksum": checksum[:16]},
    )

    return draft


def review_campaign_message_draft(
    draft: CampaignMessageDraft,
    action: str,
    reviewer=None,
    rejection_reason: str = "",
) -> CampaignMessageDraft:
    """
    Review action handler for CampaignMessageDraft.
    Action choices: 'approve', 'reject', 'edit'.
    CRITICAL INVARIANT: Approval NEVER sets sendable=True or external_action_authorized=True.
    """
    event_type = "draft_review_approved"
    if action == "approve":
        draft.status = "approved"
        draft.reviewer = reviewer
        draft.reviewed_at = timezone.now()
        event_type = "draft_review_approved"
    elif action == "reject":
        draft.status = "rejected"
        draft.reviewer = reviewer
        draft.reviewed_at = timezone.now()
        draft.rejection_reason = rejection_reason
        event_type = "draft_review_rejected"
    elif action == "edit":
        draft.status = "reviewed"
        draft.reviewer = reviewer
        draft.reviewed_at = timezone.now()
        event_type = "draft_review_edited"

    # Enforce non-sendable invariants unconditionally
    draft.draft_mode = True
    draft.sendable = False
    draft.external_action_authorized = False
    draft.save()

    record_audit_event(
        workspace=draft.workspace,
        event_type=event_type,
        actor=reviewer,
        target=draft,
        action_key=f"draft_review_{draft.id}_{action}",
        metadata={"status": draft.status, "action": action},
    )

    return draft
