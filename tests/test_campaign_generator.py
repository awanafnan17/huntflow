import pytest
from django.core.exceptions import ValidationError

from openoutreach.core.models import (
    Workspace,
    Campaign,
    CampaignPurpose,
    SenderIdentity,
    MessageSequenceStep,
    CampaignMessageDraft,
)
from openoutreach.core.services.campaign_wizard import create_campaign_from_wizard
from openoutreach.core.services.campaign_generator import (
    generate_draft_for_deal_step,
    review_campaign_message_draft,
)
from openoutreach.crm.models.lead import Lead
from openoutreach.crm.models.deal import Deal, DealState
from tests.factories import UserFactory


@pytest.mark.django_db
class TestCampaignGeneratorAndWizard:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.user = UserFactory(username="wizard_user")
        self.ws = Workspace.objects.create(name="Wizard WS", slug="wizard-ws")
        self.job_purpose = CampaignPurpose.objects.get(slug="job_outreach", is_system=True)
        self.sender = SenderIdentity.objects.create(
            workspace=self.ws,
            display_name="Sender Alice",
            email="alice@wizard.com",
            verification_status="verified",
        )

    def test_campaign_wizard_creation(self):
        camp = create_campaign_from_wizard(
            workspace=self.ws,
            campaign_purpose=self.job_purpose,
            sender_identity=self.sender,
            name="Wizard Job Campaign",
            user=self.user,
        )
        assert camp.pk is not None
        assert camp.workspace == self.ws
        assert camp.sender_identity == self.sender
        assert camp.sequence_steps.count() == 3

    def test_draft_generation_pipeline_and_invariants(self):
        camp = create_campaign_from_wizard(
            workspace=self.ws,
            campaign_purpose=self.job_purpose,
            sender_identity=self.sender,
            name="Draft Campaign",
            user=self.user,
        )
        lead = Lead.objects.create(
            workspace=self.ws,
            public_identifier="lead_bob",
            first_name="Bob",
            company="Acme Corp",
        )
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()

        context_data = {
            "recipient_name": "Bob",
            "recipient_role": "Engineering Lead",
            "company_name": "Acme Corp",
            "reason_for_connecting": "Impressed by Acme's AI work",
            "verified_candidate_facts": ["10 years AI engineering"],
        }

        draft = generate_draft_for_deal_step(
            deal=deal,
            sequence_step=step,
            context_data=context_data,
            user=self.user,
        )

        assert draft.pk is not None
        assert draft.status == "draft"
        # STRICT SAFETY INVARIANTS
        assert draft.draft_mode is True
        assert draft.sendable is False
        assert draft.external_action_authorized is False

    def test_draft_review_actions_maintain_non_sendable_invariants(self):
        camp = create_campaign_from_wizard(
            workspace=self.ws,
            campaign_purpose=self.job_purpose,
            sender_identity=self.sender,
            name="Review Campaign",
            user=self.user,
        )
        lead = Lead.objects.create(workspace=self.ws, public_identifier="lead_review", first_name="Charlie")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()

        draft = generate_draft_for_deal_step(
            deal=deal,
            sequence_step=step,
            context_data={
                "recipient_name": "Charlie",
                "recipient_role": "CTO",
                "company_name": "Beta Inc",
                "reason_for_connecting": "Great technical blog posts",
                "verified_candidate_facts": [],
            },
            user=self.user,
        )

        # Action: Approve draft
        approved_draft = review_campaign_message_draft(draft, action="approve", reviewer=self.user)
        assert approved_draft.status == "approved"
        # MUST REMAIN STRICTLY UNTRANSMITTABLE
        assert approved_draft.draft_mode is True
        assert approved_draft.sendable is False
        assert approved_draft.external_action_authorized is False
