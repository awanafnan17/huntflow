# openoutreach/core/services/campaign_wizard.py
from typing import Optional
from django.core.exceptions import ValidationError

from openoutreach.core.models import (
    Workspace,
    Campaign,
    CampaignPurpose,
    SenderIdentity,
    MessageSequenceStep,
    CampaignPurposeSequenceTemplate,
)


from openoutreach.core.services.entitlements import consume_entitlement
from openoutreach.core.services.audit_service import record_audit_event


def create_campaign_from_wizard(
    workspace: Workspace,
    campaign_purpose: CampaignPurpose,
    sender_identity: Optional[SenderIdentity] = None,
    name: str = "",
    objective: str = "",
    target_audience: str = "",
    user=None,
    is_synthetic: bool = False,
    synthetic_run=None,
) -> Campaign:
    """
    Step-by-step Campaign Setup Wizard Service.
    Instantiates campaign, links purpose/sender, copies default sequence templates into campaign sequence steps.
    Enforces workspace scoping & RBAC.
    """
    action_key = f"camp_create_{workspace.id}_{hash(name) % 100000}"
    consume_entitlement(workspace=workspace, metric="campaigns", quantity=1, action_key=action_key)

    if not campaign_purpose.is_system and campaign_purpose.workspace != workspace:
        raise ValidationError("Selected campaign purpose does not belong to this workspace.")

    if sender_identity and sender_identity.workspace != workspace:
        raise ValidationError("Selected sender identity does not belong to this workspace.")

    if sender_identity and campaign_purpose not in sender_identity.allowed_purposes.all() and not campaign_purpose.is_system:
        raise ValidationError("Sender identity is not authorized for this campaign purpose.")

    campaign = Campaign.objects.create(
        workspace=workspace,
        campaign_purpose=campaign_purpose,
        sender_identity=sender_identity,
        campaign_type=campaign_purpose.slug,
        name=name or f"{campaign_purpose.name} Campaign",
        campaign_objective=objective or campaign_purpose.objective,
        target_audience_definition=target_audience,
        owner=user,
        is_synthetic=is_synthetic,
        synthetic_run=synthetic_run,
    )

    record_audit_event(
        workspace=workspace,
        event_type="campaign_created",
        actor=user,
        target=campaign,
        action_key=f"camp_{campaign.id}",
        metadata={"name": campaign.name, "purpose": campaign_purpose.slug},
    )

    # Copy sequence templates into campaign sequence steps
    templates = CampaignPurposeSequenceTemplate.objects.filter(campaign_purpose=campaign_purpose)
    for tmpl in templates:
        step, created = MessageSequenceStep.objects.get_or_create(
            campaign=campaign,
            sequence_number=tmpl.sequence_number,
            defaults={
                "channel": tmpl.channel,
                "step_type": tmpl.step_type,
                "delay_hours": tmpl.delay_hours,
                "length_limit": tmpl.length_limit,
                "approval_requirement": tmpl.approval_requirement,
                "sending_policy": tmpl.sending_policy,
                "is_enabled": True,
            }
        )
        if created:
            record_audit_event(
                workspace=workspace,
                event_type="campaign_sequence_instantiated",
                actor=user,
                target=step,
                action_key=f"seq_{step.id}",
                metadata={"sequence_number": tmpl.sequence_number, "channel": tmpl.channel},
            )

    return campaign
