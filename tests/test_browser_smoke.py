import pytest
from django.urls import reverse

from openoutreach.core.models import (
    Workspace,
    Campaign,
    CampaignPurpose,
    SenderIdentity,
    CampaignMessageDraft,
)
from openoutreach.core.services.workspace_membership import create_workspace_with_owner
from openoutreach.core.services.campaign_wizard import create_campaign_from_wizard
from openoutreach.core.services.campaign_generator import generate_draft_for_deal_step
from openoutreach.crm.models.lead import Lead
from openoutreach.crm.models.deal import Deal, DealState
from tests.factories import UserFactory


@pytest.mark.django_db
class TestBrowserSmokeSuite:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.staff_user = UserFactory(username="smoke_staff", is_staff=True, is_superuser=True)
        self.ws = create_workspace_with_owner(name="Smoke WS", slug="smoke-ws", owner_user=self.staff_user)
        self.job_purpose = CampaignPurpose.objects.get(slug="job_outreach", is_system=True)

    def test_browser_onboarding_smoke(self, client):
        client.force_login(self.staff_user)
        res = client.get(reverse("dashboard:onboarding_start"))
        assert res.status_code == 200

        res_post = client.post(reverse("dashboard:onboarding_start"), {"name": "New Smoke WS", "slug": "new-smoke-ws"})
        assert res_post.status_code == 302

    def test_browser_campaign_wizard_smoke(self, client):
        client.force_login(self.staff_user)
        camp = create_campaign_from_wizard(
            workspace=self.ws,
            campaign_purpose=self.job_purpose,
            name="Smoke Campaign",
            user=self.staff_user,
        )
        assert camp.pk is not None

    def test_browser_target_import_preview_smoke(self, client):
        client.force_login(self.staff_user)
        res = client.get(reverse("dashboard:import_upload", kwargs={"workspace_id": self.ws.id}))
        assert res.status_code == 200

    def test_browser_draft_review_smoke(self, client):
        client.force_login(self.staff_user)
        camp = create_campaign_from_wizard(workspace=self.ws, campaign_purpose=self.job_purpose)
        lead = Lead.objects.create(workspace=self.ws, public_identifier="smoke_lead", first_name="Smoke Target")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()

        draft = generate_draft_for_deal_step(
            deal=deal,
            sequence_step=step,
            context_data={"recipient_name": "Smoke Target", "recipient_role": "Lead Engineer", "company_name": "Acme", "reason_for_connecting": "Testing", "verified_candidate_facts": ["10 years experience"]},
        )

        res_list = client.get(reverse("dashboard:draft_list", kwargs={"workspace_id": self.ws.id}))
        assert res_list.status_code == 200

        res_detail = client.get(reverse("dashboard:draft_detail", kwargs={"workspace_id": self.ws.id, "draft_id": draft.id}))
        assert res_detail.status_code == 200

        res_action = client.post(
            reverse("dashboard:draft_detail", kwargs={"workspace_id": self.ws.id, "draft_id": draft.id}),
            {"action": "approve"},
        )
        assert res_action.status_code == 302
        draft.refresh_from_db()
        assert draft.status == "approved"
        assert draft.sendable is False
        assert draft.external_action_authorized is False
