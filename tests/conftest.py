# tests/conftest.py
import pytest
from unittest.mock import MagicMock
from factory.django import DjangoModelFactory
from django.contrib.auth.models import User


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User

    username = "testuser"
    email = "test@example.com"


class FakeAccountSession:
    """Mock AccountSession compatible with core services."""

    def __init__(self, django_user, linkedin_profile, campaign):
        self.django_user = django_user
        self.linkedin_profile = linkedin_profile
        self.campaign = campaign
        self.page = MagicMock()
        self.page.content = MagicMock(return_value="")
        self.context = MagicMock()
        self.context.cookies = MagicMock(return_value=[])

    @property
    def is_authenticated(self):
        return True

    @property
    def is_session_healthy(self):
        return True

    @property
    def active_timezone(self):
        return "UTC"

    def get_public_id(self):
        return self.linkedin_profile.linkedin_username if self.linkedin_profile else "testuser"

    @property
    def campaigns(self):
        from openoutreach.core.models import Campaign
        return Campaign.objects.filter(users=self.django_user)

    def ensure_browser(self):
        pass


@pytest.fixture
def fake_session(db):
    """An AccountSession-like object backed by the Django test DB with an explicitly verified valid test profile and bound campaign."""
    from openoutreach.core.models import Campaign, Workspace, WorkspaceAutomationSettings
    from openoutreach.linkedin.models import LinkedInProfile

    user = UserFactory(username="testuser")
    workspace, _ = Workspace.objects.get_or_create(name="Default Test Workspace", slug="default-test-ws")
    auto_st = WorkspaceAutomationSettings.get_for_workspace(workspace)
    if auto_st:
        auto_st.master_automation_enabled = True
        auto_st.auto_send_connections = True
        auto_st.auto_followup = True
        auto_st.auto_send_emails = True
        auto_st.auto_apply_jobs = True
        auto_st.daily_connections_target = 100
        auto_st.daily_outbound_limit = 100
        auto_st.max_daily_connections = 100
        auto_st.max_daily_follow_ups = 100
        auto_st.max_daily_messages = 100
        auto_st.provider_safety_state = "NORMAL"
        auto_st.soak_status = "NORMAL"
        auto_st.save()

    campaign = Campaign.objects.first()
    if campaign is None:
        campaign = Campaign.objects.create(name="LinkedIn Outreach", workspace=workspace)
    elif not campaign.workspace:
        campaign.workspace = workspace
        campaign.save()
    campaign.users.add(user)

    linkedin_profile, _ = LinkedInProfile.objects.get_or_create(
        user=user,
        defaults={
            "linkedin_username": "testuser@example.com",
            "linkedin_password": "testpass",
            "workspace": workspace,
            "active": True,
            "session_status": "valid",
            "identity_validation_status": "VALID_IDENTITY_MATCH",
        },
    )
    if not linkedin_profile.workspace:
        linkedin_profile.workspace = workspace
    if not linkedin_profile.active or linkedin_profile.session_status != "valid" or linkedin_profile.identity_validation_status != "VALID_IDENTITY_MATCH":
        linkedin_profile.active = True
        linkedin_profile.session_status = "valid"
        linkedin_profile.identity_validation_status = "VALID_IDENTITY_MATCH"
    linkedin_profile.save()

    if campaign.linkedin_profile_id != linkedin_profile.id:
        campaign.linkedin_profile = linkedin_profile
        campaign.save()

    return FakeAccountSession(django_user=user, linkedin_profile=linkedin_profile, campaign=campaign)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    """Close all Django database connections cleanly before pytest destroys test DBs."""
    try:
        from django.db import connections
        connections.close_all()
    except Exception:
        pass
