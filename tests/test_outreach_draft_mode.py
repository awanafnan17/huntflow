import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.urls import reverse
from django.test import Client
from openoutreach.core.models import Campaign
from openoutreach.crm.models.deal import Deal, DealState
from openoutreach.crm.models.lead import Lead, SuppressionRecord
from openoutreach.crm.service import suppress_lead_and_deals
from openoutreach.core.content_style import validate_generated_content_claims
from openoutreach.career.models import IncidentHold
from tests.factories import UserFactory

@pytest.mark.django_db
class TestOutreachDraftMode:
    @pytest.fixture(autouse=True)
    def _setup(self, db):
        self.user = UserFactory(username="seeker_user", email="candidate-10@example.com")
        self.client = Client()
        self.client.force_login(self.user)
        
        # Create campaigns of all four types
        self.camp_job = Campaign.objects.create(name="Job Outreach", campaign_type="job_outreach", is_active=True)
        self.camp_geosentry = Campaign.objects.create(name="GeoSentry Mining", campaign_type="geosentry_mining", is_active=True)
        self.camp_freelance = Campaign.objects.create(name="Freelance", campaign_type="freelance_clients", is_active=True)
        self.camp_scholarship = Campaign.objects.create(name="Scholarship", campaign_type="masters_scholarship", is_active=True)
        
        self.lead1 = Lead.objects.create(linkedin_url="https://linkedin.com/in/lead1", public_identifier="lead1")
        self.lead2 = Lead.objects.create(linkedin_url="https://linkedin.com/in/lead2", public_identifier="lead2")
        self.lead3 = Lead.objects.create(linkedin_url="https://linkedin.com/in/lead3", public_identifier="lead3")
        self.lead4 = Lead.objects.create(linkedin_url="https://linkedin.com/in/lead4", public_identifier="lead4")

    def test_four_campaign_types_exist(self):
        types = [c[0] for c in Campaign.CAMPAIGN_TYPES]
        assert "job_outreach" in types
        assert "geosentry_mining" in types
        assert "freelance_clients" in types
        assert "masters_scholarship" in types

    def test_all_four_campaigns_can_hold_drafts_simultaneously(self):
        d1 = Deal.objects.create(campaign=self.camp_job, lead=self.lead1, state=DealState.QUALIFIED)
        d2 = Deal.objects.create(campaign=self.camp_geosentry, lead=self.lead2, state=DealState.QUALIFIED)
        d3 = Deal.objects.create(campaign=self.camp_freelance, lead=self.lead3, state=DealState.QUALIFIED)
        d4 = Deal.objects.create(campaign=self.camp_scholarship, lead=self.lead4, state=DealState.QUALIFIED)
        
        assert Deal.objects.filter(draft_mode=True).count() == 4

    def test_active_cross_campaign_duplicate_blocked(self):
        Deal.objects.create(campaign=self.camp_job, lead=self.lead1, state=DealState.QUALIFIED)
        
        # Second active deal for same lead in another campaign must be blocked
        deal2 = Deal(campaign=self.camp_geosentry, lead=self.lead1, state=DealState.QUALIFIED)
        with pytest.raises(ValidationError, match="already active in another campaign"):
            deal2.clean()

    def test_suppression_enforced_and_transition_succeeds(self):
        # 1. Suppression transition via service
        deal = Deal.objects.create(campaign=self.camp_job, lead=self.lead1, state=DealState.QUALIFIED)
        
        suppress_lead_and_deals(self.lead1.linkedin_url, reason="Opted out")
        
        # Verify deal is rejected
        deal.refresh_from_db()
        assert deal.manual_review_status == "rejected"
        assert "Suppressed" in deal.rejection_reason
        assert not deal.sendable
        
        # Mark first deal failed to avoid duplicate check conflict
        deal.state = DealState.FAILED
        deal.save()
        
        # 2. Enforces future creation blocking
        new_deal = Deal(campaign=self.camp_geosentry, lead=self.lead1, state=DealState.QUALIFIED)
        with pytest.raises(ValidationError, match="suppressed"):
            new_deal.clean()

    def test_generated_previews_are_non_sendable_even_after_approval(self):
        deal = Deal.objects.create(
            campaign=self.camp_job,
            lead=self.lead1,
            state=DealState.QUALIFIED,
            draft_mode=True,
            sendable=False,
            external_action_authorized=False,
            manual_review_status="pending"
        )
        
        # Post approval action to view
        response = self.client.post(reverse("dashboard:deal_review_action", args=[deal.pk]), {"action": "approve"})
        assert response.status_code == 302
        
        deal.refresh_from_db()
        assert deal.manual_review_status == "approved"
        # Previews are strictly non-sendable/sandboxed
        assert not deal.sendable
        assert not deal.external_action_authorized

    def test_edited_drafts_are_revalidated_and_style_failure_blocks_persistence(self):
        deal = Deal.objects.create(
            campaign=self.camp_job,
            lead=self.lead1,
            state=DealState.QUALIFIED,
            draft_mode=True
        )
        
        # Post edit with invalid style (em dash)
        response = self.client.post(reverse("dashboard:deal_review_action", args=[deal.pk]), {
            "action": "edit",
            "connection_note": "Hello — world",
            "follow_up": "Valid follow up"
        })
        
        # Verify deal was NOT updated with invalid style note
        deal.refresh_from_db()
        assert "Hello — world" not in deal.generated_connection_note

    def test_identity_gate_and_hold_blocks_transmission(self):
        # 1. Claims validation blocks when unverified/held
        claim_context = {
            "identity_unverified": True,
            "incident_hold_active": True
        }
        res = validate_generated_content_claims("Some message text", claim_context)
        assert not res["valid"]
        assert any("unverified" in err.lower() for err in res["errors"])
        assert any("incident hold" in err.lower() for err in res["errors"])
        
        # 2. View safety status populates correctly
        IncidentHold.objects.create(reason="TEST_HOLD", active=True)
        deal = Deal.objects.create(
            campaign=self.camp_job,
            lead=self.lead1,
            state=DealState.QUALIFIED,
            generated_connection_note="hey, noticed your work at Canva... curious what you're focused on rn?"
        )
        response = self.client.get(reverse("dashboard:campaign_review", args=[self.camp_job.pk]))
        assert response.status_code == 200
        
        # The view context list has safety fields on the deal
        deals_in_ctx = response.context["deals"]
        assert len(deals_in_ctx) == 1
        d_ctx = deals_in_ctx[0]
        assert d_ctx.identity_status == "Unverified"
        assert d_ctx.incident_hold_active is True
        assert d_ctx.claims_valid is False
