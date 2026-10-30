"""
Tests for Non-Negotiable External Action Authorization Gates (Batches 11 & 12).
"""
import pytest
from django.core.exceptions import ValidationError

from openoutreach.core.models import (
    Workspace, WorkspaceMembership, Campaign, CampaignPurpose,
    SenderIdentity, CampaignMessageDraft, MessageSequenceStep, AuditEvent, WorkspaceEntitlement
)
from openoutreach.career.models import IncidentHold, IncidentHoldTarget
from openoutreach.crm.models.lead import Lead, SuppressionRecord
from openoutreach.crm.models.deal import Deal
from openoutreach.core.services.entitlements import get_or_create_workspace_entitlement, check_entitlement
from openoutreach.core.services.audit_service import record_audit_event


from openoutreach.core.services.external_action_gates import evaluate_external_action_gates


@pytest.mark.django_db
class TestExternalActionGates:

    @pytest.fixture(autouse=True)
    def setup_data(self, db, django_user_model):
        self.User = django_user_model
        self.user = self.User.objects.create_user(username="gate_owner", email="gate@example.com")
        self.ws = Workspace.objects.create(name="Gate Workspace", slug="gate-ws")
        self.membership = WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")

        self.purpose = CampaignPurpose.objects.create(
            workspace=self.ws,
            name="Job Outreach",
            slug="job_outreach",
            is_system=True,
            is_active=True
        )
        self.sender = SenderIdentity.objects.create(
            workspace=self.ws,
            display_name="Gate Sender",
            email="gatesender@demo.invalid",
            verification_status="verified",
            is_active=True
        )
        self.campaign = Campaign.objects.create(
            workspace=self.ws,
            name="Job Outreach Campaign",
            campaign_purpose=self.purpose,
            sender_identity=self.sender,
            is_active=True
        )
        self.lead = Lead.objects.create(
            workspace=self.ws,
            public_identifier="gate-lead-1",
            first_name="Bob",
            last_name="Johnson",
            linkedin_url="https://linkedin.com/in/bob-johnson"
        )
        self.deal = Deal.objects.create(
            campaign=self.campaign,
            lead=self.lead,
            state="Qualified"
        )
        self.step = MessageSequenceStep.objects.create(
            campaign=self.campaign,
            sequence_number=1,
            channel="linkedin",
            step_type="connection_note"
        )
        self.draft = CampaignMessageDraft.objects.create(
            workspace=self.ws,
            campaign=self.campaign,
            deal=self.deal,
            lead=self.lead,
            sender_identity=self.sender,
            sequence_step=self.step,
            content="Nice to connect.",
            status="approved",
            draft_mode=True,
            sendable=False,
            external_action_authorized=True
        )

    def test_all_gates_pass(self):
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            membership=self.membership,
            sender=self.sender,
            campaign=self.campaign,
            lead=self.lead,
            deal=self.deal,
            draft=self.draft
        )
        assert allowed is True
        assert reason == "authorized"

    def test_gate_01_membership_inactive(self):
        self.membership.status = "suspended"
        self.membership.save()
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws, membership=self.membership, sender=self.sender,
            campaign=self.campaign, lead=self.lead, deal=self.deal, draft=self.draft
        )
        assert allowed is False
        assert reason == "membership_inactive"

    def test_gate_04_sender_inactive(self):
        self.sender.is_active = False
        self.sender.save()
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws, membership=self.membership, sender=self.sender,
            campaign=self.campaign, lead=self.lead, deal=self.deal, draft=self.draft
        )
        assert allowed is False
        assert reason == "sender_inactive"

    def test_gate_07_incident_hold_active(self):
        hold = IncidentHold.objects.create(active=True, reason="IDENTITY_AND_SOURCE_INTEGRITY_INCIDENT")
        IncidentHoldTarget.objects.create(incident_hold=hold, content_type_id=1, object_id=self.deal.id)
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws, membership=self.membership, sender=self.sender,
            campaign=self.campaign, lead=self.lead, deal=self.deal, draft=self.draft
        )
        assert allowed is False
        assert reason == "incident_hold_active"

    def test_gate_08_target_suppressed(self):
        SuppressionRecord.objects.create(workspace=self.ws, linkedin_url=self.lead.linkedin_url, reason="opt_out")
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws, membership=self.membership, sender=self.sender,
            campaign=self.campaign, lead=self.lead, deal=self.deal, draft=self.draft
        )
        assert allowed is False
        assert reason == "target_suppressed"

    def test_gate_18_external_authorization_missing(self):
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws, membership=self.membership, sender=self.sender,
            campaign=self.campaign, lead=self.lead, deal=self.deal, draft=self.draft,
            external_action_authorized=False
        )
        assert allowed is False
        assert reason == "external_authorization_missing"

    def test_gate_23_captcha_detected(self):
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws, membership=self.membership, sender=self.sender,
            campaign=self.campaign, lead=self.lead, deal=self.deal, draft=self.draft,
            has_captcha=True
        )
        assert allowed is False
        assert reason == "platform_safety_signal_active"
