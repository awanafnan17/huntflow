from django.test import TestCase
from django.contrib.auth import get_user_model
from openoutreach.chat.models import AgentConversation, AgentMessage, CampaignBrief, ConnectedGoogleAccount, LeadImport, LeadImportRow
from openoutreach.core.models import Workspace, WorkspaceMembership

User = get_user_model()

class ChatPlatformTests(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(username="testuser1", password="password123")
        self.user2 = User.objects.create_user(username="testuser2", password="password123")
        self.ws1 = Workspace.objects.create(name="Workspace 1", slug="ws-1")
        self.ws2 = Workspace.objects.create(name="Workspace 2", slug="ws-2")
        WorkspaceMembership.objects.create(user=self.user1, workspace=self.ws1, role="admin")
        WorkspaceMembership.objects.create(user=self.user2, workspace=self.ws2, role="admin")

    def test_agent_conversation_creation(self):
        conv = AgentConversation.objects.create(user=self.user1, title="Dubai Founders Campaign")
        self.assertEqual(conv.title, "Dubai Founders Campaign")
        
        msg = AgentMessage.objects.create(conversation=conv, role="user", content="Connect with founders in Dubai")
        self.assertEqual(msg.role, "user")
        self.assertEqual(conv.messages.count(), 1)

    def test_campaign_brief_creation(self):
        conv = AgentConversation.objects.create(user=self.user1, title="Email Outreach")
        brief = CampaignBrief.objects.create(
            conversation=conv,
            campaign_name="Dental Website Redesign",
            campaign_type="email_outreach",
            target_regions=["United States"],
            industries=["Healthcare", "Dentistry"]
        )
        self.assertEqual(brief.campaign_name, "Dental Website Redesign")
        self.assertEqual(brief.target_regions, ["United States"])

    def test_google_account_model(self):
        acc = ConnectedGoogleAccount.objects.create(
            user=self.user1,
            email="candidate-9@example.com",
            display_name="Casey Example"
        )
        self.assertEqual(acc.email, "candidate-9@example.com")
        self.assertTrue(acc.is_active)

    def test_lead_import_rows_with_workspace_tenancy(self):
        limp = LeadImport.objects.create(workspace=self.ws1, file_name="leads.csv", total_rows=2, successful_rows=2)
        row1 = LeadImportRow.objects.create(lead_import=limp, row_number=1, email="lead1@example.com", company="TechCorp")
        row2 = LeadImportRow.objects.create(lead_import=limp, row_number=2, email="lead2@example.com", company="DentalInc")
        self.assertEqual(limp.rows.count(), 2)
        self.assertEqual(row1.email, "lead1@example.com")
        
        # Verify cross-workspace isolation
        ws2_imports = LeadImport.objects.filter(workspace=self.ws2)
        self.assertEqual(ws2_imports.count(), 0)

