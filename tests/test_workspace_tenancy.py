import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib.auth.models import User
from django.http import Http404

from openoutreach.core.models import Workspace, WorkspaceMembership, Campaign
from openoutreach.crm.models.lead import Lead, SuppressionRecord
from openoutreach.crm.models.deal import Deal, DealState
from openoutreach.core.permissions import (
    get_workspace_for_user,
    has_workspace_permission,
    require_workspace_permission,
    require_workspace_membership,
    get_workspace_object_or_404,
    ROLE_PERMISSIONS,
)
from openoutreach.core.services.workspace_membership import (
    change_workspace_member_role,
    remove_workspace_member,
    leave_workspace,
    suspend_workspace_member,
    transfer_workspace_ownership,
)
from openoutreach.crm.services.deal_service import create_deal, update_deal
from tests.factories import UserFactory


@pytest.mark.django_db
class TestWorkspaceTenancy:
    @pytest.fixture(autouse=True)
    def _setup(self):
        # Users
        self.owner_user = UserFactory(username="workspace_owner", email="owner@example.com")
        self.admin_user = UserFactory(username="workspace_admin", email="admin@example.com")
        self.mgr_user = UserFactory(username="workspace_mgr", email="mgr@example.com")
        self.reviewer_user = UserFactory(username="workspace_reviewer", email="reviewer@example.com")
        self.operator_user = UserFactory(username="workspace_operator", email="operator@example.com")
        self.viewer_user = UserFactory(username="workspace_viewer", email="viewer@example.com")
        self.other_user = UserFactory(username="other_user", email="other@example.com")

        # Workspaces
        self.ws1 = Workspace.objects.create(name="Workspace 1", slug="ws1", subscription_tier="free")
        self.ws2 = Workspace.objects.create(name="Workspace 2", slug="ws2", subscription_tier="starter")

        # Memberships in WS1
        self.m1 = WorkspaceMembership.objects.create(workspace=self.ws1, user=self.owner_user, role="owner", status="active")
        self.m2 = WorkspaceMembership.objects.create(workspace=self.ws1, user=self.admin_user, role="admin", status="active")
        self.m3 = WorkspaceMembership.objects.create(workspace=self.ws1, user=self.mgr_user, role="campaign_manager", status="active")
        self.m4 = WorkspaceMembership.objects.create(workspace=self.ws1, user=self.reviewer_user, role="reviewer", status="active")
        self.m5 = WorkspaceMembership.objects.create(workspace=self.ws1, user=self.operator_user, role="operator", status="active")
        self.m6 = WorkspaceMembership.objects.create(workspace=self.ws1, user=self.viewer_user, role="viewer", status="active")

        # Memberships in WS2
        self.m7 = WorkspaceMembership.objects.create(workspace=self.ws2, user=self.other_user, role="owner", status="active")

    def test_membership_uniqueness(self):
        with pytest.raises(ValidationError):
            membership = WorkspaceMembership(workspace=self.ws1, user=self.owner_user, role="viewer")
            membership.full_clean()
            membership.save()

    def test_last_owner_cannot_be_demoted_or_removed(self):
        # In WS2, other_user is the last owner
        with pytest.raises(ValidationError, match="last owner cannot be demoted"):
            change_workspace_member_role(self.ws2, self.other_user, "admin")

        with pytest.raises(ValidationError, match="last owner cannot be removed"):
            remove_workspace_member(self.ws2, self.other_user)

        with pytest.raises(ValidationError, match="last owner cannot be removed"):
            leave_workspace(self.ws2, self.other_user)

        with pytest.raises(ValidationError, match="last owner cannot be suspended"):
            suspend_workspace_member(self.ws2, self.other_user)

    def test_transfer_workspace_ownership(self):
        # Promote admin_user to owner first via transfer_workspace_ownership
        transfer_workspace_ownership(self.ws1, current_owner=self.owner_user, new_owner=self.admin_user)
        self.m1.refresh_from_db()
        self.m2.refresh_from_db()
        assert self.m2.role == "owner"
        assert self.m1.role == "admin"

    def test_workspace_permission_checks_all_roles(self):
        # Owner
        assert has_workspace_permission(self.owner_user, self.ws1, "workspace.manage")
        assert has_workspace_permission(self.owner_user, self.ws1, "workspace.delete")

        # Admin
        assert has_workspace_permission(self.admin_user, self.ws1, "campaign.create")
        assert not has_workspace_permission(self.admin_user, self.ws1, "workspace.delete")

        # Campaign Manager
        assert has_workspace_permission(self.mgr_user, self.ws1, "campaign.create")
        assert not has_workspace_permission(self.mgr_user, self.ws1, "members.manage")

        # Reviewer
        assert has_workspace_permission(self.reviewer_user, self.ws1, "draft.review")
        assert not has_workspace_permission(self.reviewer_user, self.ws1, "campaign.create")

        # Operator
        assert has_workspace_permission(self.operator_user, self.ws1, "queue.execute")
        assert not has_workspace_permission(self.operator_user, self.ws1, "draft.approve")

        # Viewer
        assert has_workspace_permission(self.viewer_user, self.ws1, "audit.view")
        assert not has_workspace_permission(self.viewer_user, self.ws1, "campaign.create")

    def test_suspended_membership_denies_all_permissions(self):
        suspend_workspace_member(self.ws1, self.admin_user)
        assert not has_workspace_permission(self.admin_user, self.ws1, "campaign.create")
        with pytest.raises(PermissionDenied):
            get_workspace_for_user(self.admin_user, workspace_slug="ws1")
        with pytest.raises(PermissionDenied):
            require_workspace_membership(self.admin_user, self.ws1)

    def test_deal_workspace_consistency_enforced_model_and_service(self):
        camp1 = Campaign.objects.create(name="WS1 Campaign", workspace=self.ws1)
        camp2 = Campaign.objects.create(name="WS2 Campaign", workspace=self.ws2)
        lead1 = Lead.objects.create(linkedin_url="https://linkedin.com/in/lead-ws1", public_identifier="lead-ws1", workspace=self.ws1)
        lead2 = Lead.objects.create(linkedin_url="https://linkedin.com/in/lead-ws2", public_identifier="lead-ws2", workspace=self.ws2)

        # Valid deal creation
        deal = create_deal(campaign=camp1, lead=lead1)
        assert deal.pk is not None

        # Cross-workspace creation blocked by service
        with pytest.raises(ValidationError, match="Lead and Campaign must belong to the same Workspace"):
            create_deal(campaign=camp1, lead=lead2)

        # Cross-workspace update blocked by service
        with pytest.raises(ValidationError, match="Lead and Campaign must belong to the same Workspace"):
            update_deal(deal, campaign=camp2)

        with pytest.raises(ValidationError, match="Lead and Campaign must belong to the same Workspace"):
            update_deal(deal, lead=lead2)

    def test_get_workspace_object_or_404_cross_tenant_isolation(self):
        camp1 = Campaign.objects.create(name="WS1 Campaign", workspace=self.ws1)
        camp2 = Campaign.objects.create(name="WS2 Campaign", workspace=self.ws2)

        # WS1 user accessing WS1 campaign succeeds
        c = get_workspace_object_or_404(Campaign, camp1.pk, self.ws1)
        assert c == camp1

        # WS1 user accessing WS2 campaign raises 404
        with pytest.raises(Http404):
            get_workspace_object_or_404(Campaign, camp2.pk, self.ws1)

    def test_for_workspace_queryset_filtering(self):
        c1 = Campaign.objects.create(name="C1", workspace=self.ws1)
        c2 = Campaign.objects.create(name="C2", workspace=self.ws2)

        ws1_camps = list(Campaign.objects.for_workspace(self.ws1))
        ws2_camps = list(Campaign.objects.for_workspace(self.ws2))

        assert c1 in ws1_camps
        assert c2 not in ws1_camps
        assert c2 in ws2_camps
        assert c1 not in ws2_camps

    def test_dashboard_counts_excluding_other_workspaces(self):
        c1 = Campaign.objects.create(name="WS1 Camp", workspace=self.ws1, is_active=True)
        c2 = Campaign.objects.create(name="WS2 Camp", workspace=self.ws2, is_active=True)

        ws1_count = Campaign.objects.for_workspace(self.ws1).count()
        ws2_count = Campaign.objects.for_workspace(self.ws2).count()

        assert ws1_count == 1
        assert ws2_count == 1

    def test_error_messages_not_exposing_other_tenant_existence(self):
        camp2 = Campaign.objects.create(name="WS2 Secret Camp", workspace=self.ws2)
        # Attempt to access WS2 object from WS1 context should raise 404, not revealing data
        with pytest.raises(Http404):
            get_workspace_object_or_404(Campaign, camp2.pk, self.ws1)

    def test_membership_lifecycle_bypasses_blocked(self):
        # Verify direct service demotion, deletion, suspension fail on last owner
        with pytest.raises(ValidationError, match="last owner cannot be demoted"):
            change_workspace_member_role(self.ws2, self.other_user, "admin")

        with pytest.raises(ValidationError, match="last owner cannot be suspended"):
            suspend_workspace_member(self.ws2, self.other_user)

        with pytest.raises(ValidationError, match="last owner cannot be removed"):
            remove_workspace_member(self.ws2, self.other_user)

        with pytest.raises(ValidationError, match="last owner cannot be removed"):
            leave_workspace(self.ws2, self.other_user)

        # Direct model deletion also blocked
        with pytest.raises(ValidationError, match="last owner cannot be removed"):
            self.m7.delete()

