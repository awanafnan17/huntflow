# tests/test_qa_transport_guard.py
"""Safety unit tests verifying 5-point QA synthetic transport fail-closed lockdown."""
import os
import pytest
from unittest.mock import patch
from django.contrib.auth.models import User

from openoutreach.core.models import Campaign, Workspace
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.transport_guard import is_qa_synthetic_transport_allowed


@pytest.fixture
def qa_environment(db):
    user_qa = User.objects.create(username="qa_actor_user")
    user_real = User.objects.create(username="real_operator_user")

    ws_qa = Workspace.objects.create(
        slug="ws-qa-sandbox",
        name="QA Sandbox Workspace",
    )
    ws_real = Workspace.objects.create(
        slug="default",
        name="Production Workspace",
    )
    
    lp_qa = LinkedInProfile.objects.create(
        user=user_qa,
        linkedin_username="qa_synthetic_actor",
        workspace=ws_qa,
    )
    lp_real = LinkedInProfile.objects.create(
        user=user_real,
        linkedin_username="real_operator_account",
        workspace=ws_real,
    )
    
    camp_qa = Campaign.objects.create(
        name="QA Synthetic Test Campaign",
        workspace=ws_qa,
        linkedin_profile=lp_qa,
        is_synthetic=True,
    )
    camp_real = Campaign.objects.create(
        name="Real Production Outreach",
        workspace=ws_real,
        linkedin_profile=lp_real,
        is_synthetic=False,
    )

    return {
        "user_qa": user_qa,
        "user_real": user_real,
        "ws_qa": ws_qa,
        "ws_real": ws_real,
        "lp_qa": lp_qa,
        "lp_real": lp_real,
        "camp_qa": camp_qa,
        "camp_real": camp_real,
    }


def test_qa_flag_with_synthetic_campaign_and_qa_workspace_allowed(qa_environment):
    """QA flag + synthetic campaign + QA workspace + QA profile + non-prod => synthetic boundary allowed."""
    with patch.dict(os.environ, {"HUNTFLOW_QA_SYNTHETIC_TRANSPORT": "1", "DJANGO_ENV": "development", "HUNTFLOW_PRODUCTION_LOCKDOWN": "0"}):
        allowed = is_qa_synthetic_transport_allowed(
            campaign=qa_environment["camp_qa"],
            workspace=qa_environment["ws_qa"],
            linkedin_profile=qa_environment["lp_qa"],
        )
        assert allowed is True


def test_qa_flag_with_real_campaign_blocked(qa_environment):
    """QA flag + REAL campaign => synthetic boundary BLOCKED (fails closed)."""
    with patch.dict(os.environ, {"HUNTFLOW_QA_SYNTHETIC_TRANSPORT": "1", "DJANGO_ENV": "development"}):
        allowed = is_qa_synthetic_transport_allowed(
            campaign=qa_environment["camp_real"],
            workspace=qa_environment["ws_qa"],
            linkedin_profile=qa_environment["lp_qa"],
        )
        assert allowed is False


def test_qa_flag_with_real_workspace_blocked(qa_environment):
    """QA flag + REAL workspace => BLOCKED (fails closed)."""
    with patch.dict(os.environ, {"HUNTFLOW_QA_SYNTHETIC_TRANSPORT": "1", "DJANGO_ENV": "development"}):
        allowed = is_qa_synthetic_transport_allowed(
            campaign=qa_environment["camp_qa"],
            workspace=qa_environment["ws_real"],
            linkedin_profile=qa_environment["lp_qa"],
        )
        assert allowed is False


def test_qa_flag_with_real_profile_blocked(qa_environment):
    """QA flag + REAL LinkedIn profile => BLOCKED (fails closed)."""
    with patch.dict(os.environ, {"HUNTFLOW_QA_SYNTHETIC_TRANSPORT": "1", "DJANGO_ENV": "development"}):
        allowed = is_qa_synthetic_transport_allowed(
            campaign=qa_environment["camp_qa"],
            workspace=qa_environment["ws_qa"],
            linkedin_profile=qa_environment["lp_real"],
        )
        assert allowed is False


def test_qa_flag_in_production_mode_blocked(qa_environment):
    """QA flag + production mode => BLOCKED (fails closed immediately)."""
    with patch.dict(os.environ, {"HUNTFLOW_QA_SYNTHETIC_TRANSPORT": "1", "DJANGO_ENV": "production"}):
        allowed = is_qa_synthetic_transport_allowed(
            campaign=qa_environment["camp_qa"],
            workspace=qa_environment["ws_qa"],
            linkedin_profile=qa_environment["lp_qa"],
        )
        assert allowed is False

    with patch.dict(os.environ, {"HUNTFLOW_QA_SYNTHETIC_TRANSPORT": "1", "HUNTFLOW_PRODUCTION_LOCKDOWN": "1"}):
        allowed = is_qa_synthetic_transport_allowed(
            campaign=qa_environment["camp_qa"],
            workspace=qa_environment["ws_qa"],
            linkedin_profile=qa_environment["lp_qa"],
        )
        assert allowed is False


def test_no_qa_flag_blocked(qa_environment):
    """No QA flag => synthetic transport is disallowed (normal production path unchanged)."""
    env = os.environ.copy()
    env.pop("HUNTFLOW_QA_SYNTHETIC_TRANSPORT", None)
    with patch.dict(os.environ, env, clear=True):
        allowed = is_qa_synthetic_transport_allowed(
            campaign=qa_environment["camp_qa"],
            workspace=qa_environment["ws_qa"],
            linkedin_profile=qa_environment["lp_qa"],
        )
        assert allowed is False
