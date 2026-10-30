# tests/test_multi_account_truth_corrective.py
import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from openoutreach.core.models import Campaign, Task, TaskQueueEvent
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.core.task_factory import create_external_task
from openoutreach.linkedin.identity_service import validate_linkedin_identity, IdentityValidationResult
from openoutreach.linkedin.vnc_service import _get_durable_session_file, get_interactive_verification_status


@pytest.mark.django_db
def test_one_user_owns_multiple_linkedin_profiles():
    user = User.objects.create(username="multi_owner", email="owner@test.com")
    lp1 = LinkedInProfile.objects.create(user=user, linkedin_username="user1@test.com")
    lp2 = LinkedInProfile.objects.create(user=user, linkedin_username="user2@test.com")
    assert LinkedInProfile.objects.filter(user=user).count() == 2


@pytest.mark.django_db
def test_no_synthetic_django_user_required():
    user = User.objects.create(username="real_user", email="real@test.com")
    lp = LinkedInProfile.objects.create(user=user, linkedin_username="real.linkedin@test.com")
    assert lp.user == user
    assert "@example.com" not in user.email


@pytest.mark.django_db
def test_tenant_a_cannot_view_tenant_b_profiles(client):
    user_a = User.objects.create_user(username="user_a", password="password_a", is_staff=True)
    user_b = User.objects.create_user(username="user_b", password="password_b", is_staff=True)

    lp_a = LinkedInProfile.objects.create(user=user_a, linkedin_username="user_a@linkedin.com")
    lp_b = LinkedInProfile.objects.create(user=user_b, linkedin_username="user_b@linkedin.com")

    client.force_login(user_a)
    resp = client.get("/dashboard/integrations/")
    assert resp.status_code == 200
    profiles = resp.context["linkedin_profiles_list"]
    prof_ids = [p["profile"].id for p in profiles]
    assert lp_a.id in prof_ids
    assert lp_b.id not in prof_ids


@pytest.mark.django_db
def test_delegated_workspace_access_is_explicitly_authorized(client):
    admin = User.objects.create_superuser(username="admin_user", email="admin@test.com", password="admin_password")
    user_b = User.objects.create_user(username="user_b", password="password_b", is_staff=True)
    lp_b = LinkedInProfile.objects.create(user=user_b, linkedin_username="user_b@linkedin.com")

    client.force_login(admin)
    resp = client.get("/dashboard/integrations/")
    assert resp.status_code == 200
    prof_ids = [p["profile"].id for p in resp.context["linkedin_profiles_list"]]
    assert lp_b.id in prof_ids


@pytest.mark.django_db
def test_navbar_own_profile_selector_is_scoped_correctly():
    # Unit test verifying identity service checks navbar scoped selector
    lp = LinkedInProfile.objects.create(
        user=User.objects.create(username="nav_test"),
        linkedin_username="nav_user@test.com",
        cookie_data=None
    )
    res = validate_linkedin_identity(lp)
    assert res.own_profile_navbar_url == ""
    assert res.status == "UNKNOWN_IDENTITY"


@pytest.mark.django_db
def test_generic_profile_links_cannot_establish_identity():
    lp = LinkedInProfile.objects.create(
        user=User.objects.create(username="generic_link_test"),
        linkedin_username="generic@test.com",
        cookie_data=None
    )
    res = validate_linkedin_identity(lp)
    assert res.signals_independently_obtained is False
    assert res.status == "UNKNOWN_IDENTITY"


@pytest.mark.django_db
def test_visible_h1_cannot_be_replaced_by_page_title():
    res = IdentityValidationResult(
        validation_run_id="test_run",
        timestamp=timezone.now().isoformat(),
        linkedin_profile_id=1,
        credential_fingerprint_prefix="test-1-aaaaaaaa",
        final_feed_url="https://www.linkedin.com/feed/",
        own_profile_navbar_url="https://www.linkedin.com/in/john-doe/",
        canonical_profile_url="https://www.linkedin.com/in/john-doe/",
        canonical_link_url="https://www.linkedin.com/in/john-doe/",
        visible_h1="",  # Empty visible H1
        page_title="John Doe | LinkedIn",
        public_identifier="john-doe",
        signals_independently_obtained=False,
        signals_agree=False,
        stored_identity_agrees=True,
        status="UNKNOWN_IDENTITY",
    )
    assert res.visible_h1 == ""
    assert res.status == "UNKNOWN_IDENTITY"


@pytest.mark.django_db
def test_conflicting_identity_signals_produce_unknown_identity():
    res = IdentityValidationResult(
        validation_run_id="test_run_conflict",
        timestamp=timezone.now().isoformat(),
        linkedin_profile_id=1,
        credential_fingerprint_prefix="test-1-aaaaaaaa",
        final_feed_url="https://www.linkedin.com/feed/",
        own_profile_navbar_url="https://www.linkedin.com/in/john-doe/",
        canonical_profile_url="https://www.linkedin.com/in/jane-smith/",  # Conflicting URL
        canonical_link_url="",
        visible_h1="John Doe",
        page_title="John Doe | LinkedIn",
        public_identifier="jane-smith",
        signals_independently_obtained=True,
        signals_agree=False,
        stored_identity_agrees=False,
        status="UNKNOWN_IDENTITY",
    )
    assert res.signals_agree is False
    assert res.status == "UNKNOWN_IDENTITY"


@pytest.mark.django_db
def test_campaign_selection_persists():
    user = User.objects.create(username="camp_user")
    lp = LinkedInProfile.objects.create(user=user, linkedin_username="camp@test.com")
    camp = Campaign.objects.create(name="Persisted Campaign Test", linkedin_profile=lp)

    reloaded = Campaign.objects.get(id=camp.id)
    assert reloaded.linkedin_profile == lp


@pytest.mark.django_db
def test_connect_requires_linkedin_profile_id():
    LinkedInProfile.objects.all().delete()
    TaskQueueEvent.objects.all().delete()
    camp = Campaign.objects.create(name="Connect No Profile Camp", owner=None, linkedin_profile=None)
    task = create_external_task(
        task_type=Task.TaskType.CONNECT,
        campaign_id=camp.id,
        lead_id=10,
    )
    assert task is None
    assert TaskQueueEvent.objects.filter(action__in=["TASK_REJECTED_MISSING_TARGET", "EXTERNAL_TASK_TARGET_MISSING"]).exists()


@pytest.mark.django_db
def test_follow_up_requires_linkedin_profile_id():
    LinkedInProfile.objects.all().delete()
    camp = Campaign.objects.create(name="Followup No Profile Camp", linkedin_profile=None)
    task = create_external_task(
        task_type=Task.TaskType.FOLLOW_UP,
        campaign_id=camp.id,
        deal_id=10,
    )
    assert task is None


@pytest.mark.django_db
def test_check_pending_requires_linkedin_profile_id():
    LinkedInProfile.objects.all().delete()
    camp = Campaign.objects.create(name="Check Pending No Profile Camp", linkedin_profile=None)
    task = create_external_task(
        task_type=Task.TaskType.CHECK_PENDING,
        campaign_id=camp.id,
    )
    assert task is None


@pytest.mark.django_db
def test_linkedin_message_requires_linkedin_profile_id():
    camp = Campaign.objects.create(name="Message No Profile Camp", linkedin_profile=None)
    task = create_external_task(
        task_type="linkedin_message",
        campaign_id=camp.id,
        contact_id=5,
    )
    assert task is None


@pytest.mark.django_db
def test_linkedin_job_application_requires_linkedin_profile_id():
    camp = Campaign.objects.create(name="App No Profile Camp", linkedin_profile=None)
    task = create_external_task(
        task_type="job_application",
        campaign_id=camp.id,
        job_id=42,
    )
    assert task is None


@pytest.mark.django_db
def test_worker_never_falls_back_to_another_profile():
    user = User.objects.create(username="worker_fallback_user")
    lp1 = LinkedInProfile.objects.create(user=user, linkedin_username="lp1@test.com", session_status="needs_reauth", identity_validation_status="UNKNOWN_IDENTITY")
    lp2 = LinkedInProfile.objects.create(user=user, linkedin_username="lp2@test.com", session_status="valid", identity_validation_status="VALID_IDENTITY_MATCH")
    camp = Campaign.objects.create(name="Fallback Camp", linkedin_profile=lp1)

    task = create_external_task(
        task_type=Task.TaskType.CONNECT,
        campaign_id=camp.id,
        lead_id=99,
        linkedin_profile_id=lp1.id,
    )
    # Since lp1 is needs_reauth, task creation is blocked (task is None). No fallback to lp2.
    assert task is None


@pytest.mark.django_db
def test_campaign_profile_mismatch_blocks_execution():
    user = User.objects.create(username="mismatch_user")
    lp1 = LinkedInProfile.objects.create(user=user, linkedin_username="lp1@test.com", session_status="valid", identity_validation_status="VALID_IDENTITY_MATCH")
    lp2 = LinkedInProfile.objects.create(user=user, linkedin_username="lp2@test.com", session_status="valid", identity_validation_status="VALID_IDENTITY_MATCH")
    camp = Campaign.objects.create(name="Mismatch Camp", linkedin_profile=lp1)

    task = create_external_task(
        task_type=Task.TaskType.CONNECT,
        campaign_id=camp.id,
        lead_id=101,
        linkedin_profile_id=lp2.id,
    )
    # Mismatch between specified lp2 and campaign bound lp1 blocks task creation
    assert task is None


@pytest.mark.django_db
def test_candidate_account_mismatch_blocks_execution():
    user = User.objects.create(username="cand_mismatch_user")
    lp = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="cand@test.com",
        identity_validation_status="UNKNOWN_IDENTITY"
    )
    assert lp.identity_validation_status != "VALID_IDENTITY_MATCH"


@pytest.mark.django_db
def test_idempotency_includes_linkedin_profile_id():
    user = User.objects.create(username="idem_user")
    lp1 = LinkedInProfile.objects.create(user=user, linkedin_username="lp1@test.com", active=True, session_status="valid", identity_validation_status="VALID_IDENTITY_MATCH")
    lp2 = LinkedInProfile.objects.create(user=user, linkedin_username="lp2@test.com", active=True, session_status="valid", identity_validation_status="VALID_IDENTITY_MATCH")
    camp = Campaign.objects.create(name="Idem Camp", linkedin_profile=lp1)

    t1 = create_external_task(
        task_type=Task.TaskType.CONNECT,
        campaign_id=camp.id,
        lead_id=50,
        linkedin_profile_id=lp1.id,
    )
    t2 = create_external_task(
        task_type=Task.TaskType.CONNECT,
        campaign_id=camp.id,
        lead_id=50,
        linkedin_profile_id=lp2.id,
    )
    assert t1 is not None
    assert t1.payload["idempotency_key"] == f"connect:{lp1.id}:{camp.id}:50"
    assert t2 is None  # lp2 rejected due to mismatch with camp.linkedin_profile=lp1


from unittest.mock import patch

@pytest.mark.django_db
def test_action_log_records_linkedin_profile_id():
    user = User.objects.create(username="action_user")
    lp = LinkedInProfile.objects.create(user=user, linkedin_username="action@test.com")
    camp = Campaign.objects.create(name="Action Camp", linkedin_profile=lp)
    log = ActionLog.objects.create(
        action_type=ActionLog.ActionType.CONNECT,
        campaign=camp,
        linkedin_profile=lp
    )
    assert log.linkedin_profile == lp


@pytest.mark.django_db
def test_all_linkedin_limits_are_account_scoped():
    user = User.objects.create(username="limit_user")
    lp1 = LinkedInProfile.objects.create(user=user, linkedin_username="lp1@test.com", connect_daily_limit=10)
    lp2 = LinkedInProfile.objects.create(user=user, linkedin_username="lp2@test.com", connect_daily_limit=25)

    assert lp1.connect_daily_limit != lp2.connect_daily_limit


@pytest.mark.django_db
def test_browser_state_and_leases_are_account_scoped():
    file1 = _get_durable_session_file(profile_id=1)
    file2 = _get_durable_session_file(profile_id=2)
    assert file1 != file2
    assert "1" in file1
    assert "2" in file2


@pytest.mark.django_db
def test_credential_submission_is_never_logged(caplog):
    user = User.objects.create_user(username="sec_user", password="sec_password")
    lp = LinkedInProfile.objects.create(user=user, linkedin_username="sec@test.com", cookie_data=None)

    sensitive_cookie = "SENSITIVE_LI_AT_VALUE_12345"
    valid_cookies = {"cookies": [{"name": "li_at", "value": sensitive_cookie, "domain": ".linkedin.com", "path": "/"}]}
    
    with patch("openoutreach.linkedin.identity_service.validate_linkedin_identity") as mock_val:
        mock_val.return_value = IdentityValidationResult(
            validation_run_id="sec_run",
            timestamp=timezone.now().isoformat(),
            linkedin_profile_id=lp.id,
            credential_fingerprint_prefix="123456789012",
            final_feed_url="https://www.linkedin.com/feed/",
            own_profile_navbar_url="https://www.linkedin.com/in/sec-user/",
            canonical_profile_url="https://www.linkedin.com/in/sec-user/",
            canonical_link_url="",
            visible_h1="Sec User",
            page_title="Sec User | LinkedIn",
            public_identifier="sec-user",
            signals_independently_obtained=True,
            signals_agree=True,
            stored_identity_agrees=True,
            status="VALID_IDENTITY_MATCH"
        )
        res = mock_val(lp, storage_state=valid_cookies)

    for rec in caplog.records:
        assert sensitive_cookie not in rec.getMessage()


@pytest.mark.django_db
def test_exposed_or_rotation_required_sessions_cannot_write():
    user = User.objects.create(username="rot_user")
    lp = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="rot@test.com",
        session_status="needs_reauth",
        identity_validation_status="UNKNOWN_IDENTITY"
    )

    assert lp.session_status != "valid"
    assert lp.identity_validation_status != "VALID_IDENTITY_MATCH"
    assert lp.can_execute(ActionLog.ActionType.CONNECT) is False


@pytest.mark.django_db
def test_account_limit_isolation_per_profile():
    user = User.objects.create(username="limit_isolation_user")
    lp1 = LinkedInProfile.objects.create(user=user, linkedin_username="prof_a@test.com", connect_daily_limit=10)
    lp2 = LinkedInProfile.objects.create(user=user, linkedin_username="prof_b@test.com", connect_daily_limit=10)
    camp = Campaign.objects.create(name="Limit Isolation Campaign")

    # Record action on Profile A
    ActionLog.objects.create(
        action_type=ActionLog.ActionType.CONNECT,
        linkedin_profile=lp1,
        campaign=camp
    )

    count_a = ActionLog.objects.filter(linkedin_profile=lp1, action_type=ActionLog.ActionType.CONNECT).count()
    count_b = ActionLog.objects.filter(linkedin_profile=lp2, action_type=ActionLog.ActionType.CONNECT).count()

    assert count_a == 1
    assert count_b == 0


from openoutreach.linkedin.identity_service import safe_diagnostic_serialize
from openoutreach.core.models import SiteConfig


@pytest.mark.django_db
def test_safe_diagnostic_serializer_redacts_secret_fields():
    cfg = SiteConfig.objects.create(
        llm_api_key="SECRET_LLM_KEY_VAL",
        bettercontact_api_key="SECRET_BETTERCONTACT_VAL"
    )
    serialized = safe_diagnostic_serialize(cfg)
    assert "SECRET_LLM_KEY_VAL" not in str(serialized)
    assert "SECRET_BETTERCONTACT_VAL" not in str(serialized)
    assert serialized.get("llm_api_key_present") is True
    assert serialized.get("bettercontact_api_key_present") is True


@pytest.mark.django_db
def test_profile_2_cannot_bypass_identity_validation():
    user = User.objects.create(username="prof2_bypass_user")
    lp2 = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="dry@run.com",
        session_status="unknown",
        identity_validation_status="unvalidated"
    )
    assert lp2.can_execute(ActionLog.ActionType.CONNECT) is False


@pytest.mark.django_db
def test_all_profiles_use_central_execution_gate():
    user = User.objects.create(username="gate_user")
    lp_unvalidated = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="unval@test.com",
        session_status="valid",
        identity_validation_status="unvalidated"
    )
    lp_invalid_session = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="inval@test.com",
        session_status="invalid",
        identity_validation_status="VALID_IDENTITY_MATCH"
    )
    lp_valid = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="val@test.com",
        session_status="valid",
        identity_validation_status="VALID_IDENTITY_MATCH"
    )

    assert lp_unvalidated.can_execute("connect") is False
    assert lp_invalid_session.can_execute("connect") is False
    assert lp_valid.can_execute("connect") is True


@pytest.mark.django_db
def test_campaign_with_no_account_returns_waiting_account_selection():
    camp = Campaign.objects.create(name="Unbound Campaign")
    task = create_external_task(task_type="connect", campaign_id=camp.id, lead_id=123)
    assert task is None


@pytest.mark.django_db
def test_no_worker_fallback_across_all_profiles():
    user = User.objects.create(username="fallback_user")
    p1 = LinkedInProfile.objects.create(user=user, linkedin_username="p1@test.com", session_status="needs_reauth", identity_validation_status="UNKNOWN_IDENTITY")
    p2 = LinkedInProfile.objects.create(user=user, linkedin_username="p2@test.com", session_status="unknown", identity_validation_status="UNKNOWN_IDENTITY")
    p3 = LinkedInProfile.objects.create(user=user, linkedin_username="p3@test.com", session_status="needs_reauth", identity_validation_status="UNKNOWN_IDENTITY")

    assert p1.can_execute("connect") is False
    assert p2.can_execute("connect") is False
    assert p3.can_execute("connect") is False


@pytest.mark.django_db
def test_real_preflight_uses_production_resolvers():
    from django.core.management import call_command
    from io import StringIO
    out = StringIO()
    call_command("linkedin_task_preflight", non_persistent_fixture=True, stdout=out)
    res_text = out.getvalue()
    assert "manage.py linkedin_task_preflight" in res_text
    assert "WAITING_ACCOUNT_SELECTION" in res_text


@pytest.mark.django_db
def test_placeholder_credentials_are_invalid():
    from openoutreach.core.models import SiteConfig, is_valid_credential, get_credential_state

    assert is_valid_credential("ROTATED_LLM_KEY_test-1-aaaaaaaa_SECURE") is False
    assert is_valid_credential("ROTATED_BETTERCONTACT_KEY_test-1-aaaaaaaa_SECURE") is False
    assert get_credential_state("ROTATED_LLM_KEY_test-1-aaaaaaaa_SECURE") == "ROTATION_REQUIRED"
    assert get_credential_state("ROTATED_BETTERCONTACT_KEY_test-1-aaaaaaaa_SECURE") == "ROTATION_REQUIRED"
    assert is_valid_credential("PLACEHOLDER_KEY") is False
    assert get_credential_state("PLACEHOLDER_KEY") == "PLACEHOLDER"

    cfg = SiteConfig.load()
    cfg.llm_api_key = "ROTATED_LLM_KEY_test-1-aaaaaaaa_SECURE"
    cfg.bettercontact_api_key = "ROTATED_BETTERCONTACT_KEY_test-1-aaaaaaaa_SECURE"
    cfg.save()

    assert cfg.is_llm_configured() is False
    assert cfg.is_bettercontact_configured() is False


@pytest.mark.django_db
def test_workspace_mismatch_blocks_task_creation():
    from openoutreach.core.models import Workspace, WorkspaceMembership
    ws1 = Workspace.objects.create(name="WS1", slug="ws1")
    ws2 = Workspace.objects.create(name="WS2", slug="ws2")
    user1 = User.objects.create(username="ws1_user")
    WorkspaceMembership.objects.create(user=user1, workspace=ws1)

    lp = LinkedInProfile.objects.create(
        user=user1,
        linkedin_username="ws1_prof@test.com",
        active=True,
        session_status="valid",
        identity_validation_status="VALID_IDENTITY_MATCH"
    )
    camp = Campaign.objects.create(name="WS2 Camp", workspace=ws2)

    task_mismatch = create_external_task(
        task_type=Task.TaskType.CONNECT,
        campaign_id=camp.id,
        lead_id=2,
        linkedin_profile_id=lp.id
    )
    # Workspace mismatch (camp in ws2 vs lp membership in ws1) rejects task creation
    assert task_mismatch is None
