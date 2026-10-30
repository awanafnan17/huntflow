# tests/test_saas_phase5_operations.py
import pytest
from django.urls import reverse
from django.core.exceptions import ValidationError

from openoutreach.core.models import (
    Workspace,
    SubscriptionPlan,
    WorkspaceEntitlement,
    UsageMeter,
    WorkspaceInvitation,
    WorkspaceMembership,
)
from openoutreach.core.services.workspace_membership import create_workspace_with_owner
from openoutreach.core.services.entitlements import (
    seed_default_subscription_plans,
    get_or_create_workspace_entitlement,
    check_entitlement,
    record_usage,
    get_workspace_usage_summary,
)
from openoutreach.core.services.workspace_invitation import (
    create_invitation,
    accept_invitation,
    revoke_invitation,
    generate_email_verification_token,
    verify_email_token,
    generate_password_reset_token,
    confirm_password_reset,
)
from openoutreach.core.services.billing_provider import get_billing_provider
from tests.factories import UserFactory


@pytest.mark.django_db
class TestSaaSPhase5Operations:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.owner = UserFactory(username="p5_owner", email="owner@p5.org")
        self.user2 = UserFactory(username="p5_user2", email="user2@p5.org")
        self.ws = create_workspace_with_owner(name="Phase 5 Workspace", slug="p5-ws", owner_user=self.owner)
        self.plans = seed_default_subscription_plans()

    def test_health_check_endpoint(self, client):
        url = reverse("root_health_check")
        res = client.get(url)
        assert res.status_code == 200
        assert res.json()["status"] == "healthy"

    def test_readiness_check_endpoint(self, client):
        url = reverse("root_readiness_check")
        res = client.get(url)
        assert res.status_code == 200
        assert res.json()["status"] == "ready"
        assert res.json()["database"] == "connected"

    def test_readiness_check_database_unavailable(self, client, monkeypatch):
        from django.db import connections
        def mock_cursor():
            raise Exception("Database connection lost")
        monkeypatch.setattr(connections["default"], "cursor", mock_cursor)

        url = reverse("root_readiness_check")
        res = client.get(url)
        assert res.status_code == 503
        assert res.json()["status"] == "not_ready"
        assert res.json()["reason_code"] == "database_unavailable"

    def test_readiness_check_migrations_pending(self, client, monkeypatch):
        from django.db.migrations.executor import MigrationExecutor
        monkeypatch.setattr(MigrationExecutor, "migration_plan", lambda self, targets: [("core", "0036_dummy")])

        url = reverse("root_readiness_check")
        res = client.get(url)
        assert res.status_code == 503
        assert res.json()["status"] == "not_ready"
        assert res.json()["reason_code"] == "migrations_pending"

    def test_readiness_check_configuration_missing(self, client, settings):
        settings.TOKEN_PEPPER = ""
        url = reverse("root_readiness_check")
        res = client.get(url)
        assert res.status_code == 503
        assert res.json()["status"] == "not_ready"
        assert res.json()["reason_code"] == "configuration_missing"

    def test_seed_default_subscription_plans(self):
        assert "starter" in self.plans
        assert "professional" in self.plans
        assert "enterprise" in self.plans
        assert self.plans["starter"].monthly_price_usd == 49.00
        assert self.plans["professional"].monthly_price_usd == 149.00

    def test_get_or_create_workspace_entitlement(self):
        ent = get_or_create_workspace_entitlement(self.ws)
        assert ent.workspace == self.ws
        assert ent.plan.slug == "starter"
        assert ent.status == "trialing"

    def test_check_entitlement_within_limits(self):
        assert check_entitlement(self.ws, "campaigns", requested_quantity=1) is True

    def test_entitlement_status_matrix(self):
        ent = get_or_create_workspace_entitlement(self.ws)

        # 1. Trialing & Active
        ent.status = "trialing"
        ent.save()
        assert check_entitlement(self.ws, "campaigns", requested_quantity=1) is True

        ent.status = "active"
        ent.save()
        assert check_entitlement(self.ws, "campaigns", requested_quantity=1) is True

        # 2. Past Due (Blocked for billable metrics)
        ent.status = "past_due"
        ent.save()
        with pytest.raises(ValidationError, match="subscription_past_due"):
            check_entitlement(self.ws, "campaigns", requested_quantity=1)

        # 3. Expired, Suspended, Canceled
        for st in ["expired", "suspended", "canceled"]:
            ent.status = st
            ent.save()
            with pytest.raises(ValidationError, match=f"subscription_{st}"):
                check_entitlement(self.ws, "campaigns", requested_quantity=1)

        # 4. Unlimited Plan
        ent.status = "active"
        ent.plan = None
        ent.save()
        from openoutreach.core.services.entitlements import get_entitlement_decision
        decision = get_entitlement_decision(self.ws, "campaigns")
        assert decision["allowed"] is True
        assert decision["reason_code"] == "unlimited"

    def test_check_entitlement_exceeds_member_limit(self):
        ent = get_or_create_workspace_entitlement(self.ws)
        ent.plan.max_members = 1
        ent.plan.save()

        with pytest.raises(ValidationError, match="Entitlement denied|Member limit reached"):
            check_entitlement(self.ws, "members", requested_quantity=1)

    def test_record_usage_metering(self):
        meter1 = record_usage(self.ws, "leads_imported", quantity=100)
        assert meter1.count == 100

        meter2 = record_usage(self.ws, "leads_imported", quantity=50)
        assert meter2.count == 150

    def test_get_workspace_usage_summary(self):
        record_usage(self.ws, "leads_imported", quantity=250)
        summary = get_workspace_usage_summary(self.ws)
        assert summary["workspace_slug"] == "p5-ws"
        assert summary["plan_slug"] == "starter"
        assert summary["leads_imported_this_period"] == 250

    def test_create_and_accept_workspace_invitation(self):
        invite, raw_token = create_invitation(self.ws, "invitee@p5.org", role="campaign_manager", invited_by=self.owner)
        assert invite.status == "pending"
        assert raw_token is not None

        self.user2.email = "invitee@p5.org"
        self.user2.save()
        membership = accept_invitation(raw_token, self.user2)
        assert membership.workspace == self.ws
        assert membership.user == self.user2
        assert membership.role == "campaign_manager"
        assert membership.status == "active"

        invite.refresh_from_db()
        assert invite.status == "accepted"

    def test_revoke_workspace_invitation(self):
        invite, raw_token = create_invitation(self.ws, "revoke@p5.org", role="operator", invited_by=self.owner)
        revoked = revoke_invitation(invite.id, self.ws, actor=self.owner)
        assert revoked.status == "revoked"

    def test_email_verification_token_flow(self):
        tok_obj, raw_token = generate_email_verification_token(self.user2, "verify@p5.org")
        assert tok_obj.is_verified is False

        confirmed = verify_email_token(raw_token)
        assert confirmed.is_verified is True

    def test_password_reset_token_flow(self):
        tok_obj, raw_token = generate_password_reset_token(self.user2)
        assert tok_obj.is_used is False

        updated_user = confirm_password_reset(raw_token, "NewSecurePassword123!")
        assert updated_user.check_password("NewSecurePassword123!") is True
        tok_obj.refresh_from_db()
        assert tok_obj.is_used is True

    def test_mock_billing_provider_subscription_upgrade(self):
        provider = get_billing_provider()
        cus_id = provider.create_customer(self.ws, "billing@p5.org")
        assert f"cus_mock_{self.ws.slug}" in cus_id

        pro_plan = self.plans["professional"]
        ent = provider.create_subscription(self.ws, pro_plan)
        assert ent.plan == pro_plan
        assert ent.status == "active"

    def test_workspace_members_ui_and_invitation_post(self, client):
        client.force_login(self.owner)

        url_members = reverse("dashboard:workspace_members", kwargs={"workspace_id": self.ws.id})
        res = client.get(url_members)
        assert res.status_code == 200
        assert "Active Members" in res.content.decode("utf-8")

        url_invite = reverse("dashboard:workspace_invite", kwargs={"workspace_id": self.ws.id})
        res_post = client.post(url_invite, {"email": "ui_invitee@p5.org", "role": "operator"})
        assert res_post.status_code == 302
        assert WorkspaceInvitation.objects.filter(workspace=self.ws, email="ui_invitee@p5.org").exists()

    def test_workspace_billing_ui_and_mock_checkout(self, client):
        client.force_login(self.owner)

        url_billing = reverse("dashboard:workspace_billing", kwargs={"workspace_id": self.ws.id})
        res = client.get(url_billing)
        assert res.status_code == 200
        assert "Current Plan" in res.content.decode("utf-8")

        res_checkout = client.get(f"{url_billing}?plan=professional&mock_checkout=1")
        assert res_checkout.status_code == 302

        ent = get_or_create_workspace_entitlement(self.ws)
        assert ent.plan.slug == "professional"
        assert ent.status == "active"
