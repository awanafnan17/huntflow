# tests/test_rendering_saas_workflow.py
import pytest
import asyncio
from unittest.mock import MagicMock
from django.urls import reverse
from playwright.async_api import async_playwright

from openoutreach.core.models import (
    Workspace,
    Campaign,
    CampaignPurpose,
    SenderIdentity,
    CampaignMessageDraft,
    CampaignPurposeSequenceTemplate,
)
from openoutreach.core.services.workspace_membership import create_workspace_with_owner
from openoutreach.core.services.campaign_wizard import create_campaign_from_wizard
from openoutreach.core.services.campaign_generator import generate_draft_for_deal_step
from openoutreach.crm.models.lead import Lead, TargetImport
from openoutreach.crm.services.import_service import set_import_mapping
from openoutreach.crm.models.deal import Deal, DealState
from tests.factories import UserFactory


@pytest.mark.django_db(transaction=True)
class TestRenderingSaaSSmoke:
    """
    Django client HTML rendering workflow test suite for HuntFlow SaaS Draft Mode.
    Covers view rendering against synthetic data without external network calls.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.staff_user = UserFactory(username="pw_staff", is_staff=True, is_superuser=True)
        self.ws = create_workspace_with_owner(name="Playwright WS", slug="playwright-ws", owner_user=self.staff_user)
        self.job_purpose, _ = CampaignPurpose.objects.get_or_create(
            slug="job_outreach",
            defaults={"name": "Job Outreach", "is_system": True, "target_type": "individual"},
        )
        if not self.job_purpose.sequence_templates.exists():
            CampaignPurposeSequenceTemplate.objects.create(
                campaign_purpose=self.job_purpose,
                sequence_number=1,
                channel="linkedin",
                step_type="connection_note",
            )

    def test_playwright_saas_workflow_smoke(self, client):
        client.force_login(self.staff_user)

        async def _run_smoke():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()

                # 1. Login & Dashboard Page Smoke
                url_dash = reverse("dashboard:overview")
                res_dash = client.get(url_dash)
                await page.set_content(res_dash.content.decode("utf-8"))
                assert res_dash.status_code == 200

                # 2. Workspace Onboarding
                url_onboard = reverse("dashboard:onboarding_start")
                res_onboard = client.get(url_onboard)
                await page.set_content(res_onboard.content.decode("utf-8"))
                content = await page.content()
                assert "Create a Workspace" in content

                # 3. Sender Identity Creation
                url_sender = reverse("dashboard:onboarding_sender", kwargs={"workspace_id": self.ws.id})
                res_sender = client.get(url_sender)
                await page.set_content(res_sender.content.decode("utf-8"))
                content = await page.content()
                assert "Add Sender Identity" in content

                # 4. Campaign Purpose Selection
                url_purpose = reverse("dashboard:onboarding_purpose", kwargs={"workspace_id": self.ws.id})
                res_purpose = client.get(url_purpose)
                await page.set_content(res_purpose.content.decode("utf-8"))
                content = await page.content()
                assert "Select Campaign Purpose" in content

                # 5. Campaign Wizard Setup
                camp = create_campaign_from_wizard(workspace=self.ws, campaign_purpose=self.job_purpose, name="PW Campaign")
                assert camp.pk is not None

                # 6. CSV Upload UI
                url_upload = reverse("dashboard:import_upload", kwargs={"workspace_id": self.ws.id})
                res_upload = client.get(url_upload)
                await page.set_content(res_upload.content.decode("utf-8"))
                content = await page.content()
                assert "Upload Target CSV" in content

                # 7. Field Mapping UI
                imp = TargetImport.objects.create(workspace=self.ws, campaign=camp, name="PW Import", file_name="pw.csv", total_rows=1)
                url_map = reverse("dashboard:import_mapping", kwargs={"workspace_id": self.ws.id, "import_id": imp.id})
                res_map = client.get(url_map)
                await page.set_content(res_map.content.decode("utf-8"))
                content = await page.content()
                assert "Configure CSV Column Mapping" in content

                # 8. Import Preview UI (Zero Writes)
                set_import_mapping(imp, {"first_name": "Name", "email": "Email", "linkedin_url": "LinkedIn"})
                url_prev = reverse("dashboard:import_preview", kwargs={"workspace_id": self.ws.id, "import_id": imp.id})
                res_prev = client.get(url_prev)
                await page.set_content(res_prev.content.decode("utf-8"))
                content = await page.content()
                assert "Import Preview Results" in content

                # 9. Import Confirmation
                imp.status = "confirmed"
                imp.save()

                # 10. Draft List UI
                lead = Lead.objects.create(workspace=self.ws, public_identifier="pw_lead", first_name="PW Target")
                deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
                step = camp.sequence_steps.first()

                draft = generate_draft_for_deal_step(
                    deal=deal,
                    sequence_step=step,
                    context_data={
                        "recipient_name": "PW Target",
                        "recipient_role": "Lead Engineer",
                        "company_name": "PW Corp",
                        "reason_for_connecting": "AI infrastructure",
                        "verified_candidate_facts": ["10 years experience"],
                    },
                )

                url_dlist = reverse("dashboard:draft_list", kwargs={"workspace_id": self.ws.id})
                res_dlist = client.get(url_dlist)
                await page.set_content(res_dlist.content.decode("utf-8"))
                content = await page.content()
                assert "Generated Drafts" in content

                # 11. Draft Detail UI
                url_ddetail = reverse("dashboard:draft_detail", kwargs={"workspace_id": self.ws.id, "draft_id": draft.id})
                res_ddetail = client.get(url_ddetail)
                await page.set_content(res_ddetail.content.decode("utf-8"))
                content = await page.content()
                assert "Draft #" in content

                # 12. Review Approval & 13. Non-Sendable Invariant Confirmation
                res_approve = client.post(url_ddetail, {"action": "approve"})
                assert res_approve.status_code == 302
                draft.refresh_from_db()
                assert draft.status == "approved"
                assert draft.draft_mode is True
                assert draft.sendable is False
                assert draft.external_action_authorized is False

                await browser.close()

        import concurrent.futures
        from django.db import connections

        def _worker():
            try:
                asyncio.run(_run_smoke())
            finally:
                connections.close_all()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_worker)
            future.result()
