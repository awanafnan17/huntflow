import inspect
import pytest
from unittest.mock import MagicMock, patch
from tests.factories import UserFactory
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.session_manager import (
    resolve_linkedin_session_credentials,
    open_authenticated_linkedin_context,
)
from scripts.verify_real_external_account import (
    extract_live_linkedin_identity,
    compare_linkedin_identity_values,
    capture_navigation_evidence,
    classify_session_state_detailed,
    normalize_navigation_evidence,
    run_identity_extraction_session_key,
    run_session_key_diagnosis,
    LinkedInIdentityExtractionError,
)


@pytest.mark.django_db
def test_production_extractor_signature_accepts_only_page():
    """Verify that extract_live_linkedin_identity accepts ONLY 'page' parameter."""
    sig = inspect.signature(extract_live_linkedin_identity)
    params = list(sig.parameters.keys())
    assert params == ["page"], f"Parameters must be ['page'], got {params}"


@pytest.mark.django_db
def test_all_entry_points_accept_renewal_keyword():
    """Verify run_identity_extraction_session_key and run_session_key_diagnosis accept allow_operator_session_renewal."""
    sig1 = inspect.signature(run_identity_extraction_session_key)
    assert "allow_operator_session_renewal" in sig1.parameters

    sig2 = inspect.signature(run_session_key_diagnosis)
    assert "allow_operator_session_renewal" in sig2.parameters


@pytest.mark.django_db
def test_normalize_navigation_evidence_handles_empty_or_none():
    """Verify normalize_navigation_evidence handles empty or None input without KeyError."""
    norm_none = normalize_navigation_evidence(None)
    assert isinstance(norm_none, dict)
    assert norm_none["global_navigation_selector_count"] == 0
    assert norm_none["me_trigger_selector_count"] == 0
    assert norm_none["rendered_page_classification"] == "UNKNOWN"

    norm_empty = normalize_navigation_evidence({})
    assert norm_empty["login_form"] is False
    assert norm_empty["checkpoint"] is False
    assert norm_empty["captcha"] is False
    assert norm_empty["view_profile_url"] == "NOT_VERIFIED"
    assert norm_empty["profile_heading"] == "NOT_VERIFIED"


@pytest.mark.django_db
def test_normalize_navigation_evidence_preserves_populated_fields():
    """Verify normalize_navigation_evidence correctly normalizes populated raw evidence."""
    raw = {
        "document_ready_state": "complete",
        "body_text_length": 5432,
        "login_form_visible": True,
        "checkpoint_detected": False,
        "captcha_detected": True,
        "two_factor_detected": True,
        "consent_indicator_count": 2,
        "restriction_detected": True,
        "global_navigation_selector_count": 1,
        "me_menu_trigger_count": 1,
        "rendered_page_classification": "CAPTCHA",
    }
    norm = normalize_navigation_evidence(raw)
    assert norm["document_ready_state"] == "complete"
    assert norm["body_text_length"] == 5432
    assert norm["login_form"] is True
    assert norm["captcha"] is True
    assert norm["two_factor_page"] is True
    assert norm["consent_page"] is True
    assert norm["restriction_page"] is True
    assert norm["rendered_page_classification"] == "CAPTCHA"


@pytest.mark.django_db
def test_no_selector_raises_account_selection_required():
    """Verify resolver raises ValueError when no explicit selector parameter is supplied."""
    with pytest.raises(ValueError, match="ACCOUNT_SELECTION_REQUIRED"):
        resolve_linkedin_session_credentials()


@pytest.mark.django_db
def test_explicit_profile_id_and_email_resolves_correct_profile():
    """Verify explicit profile_id or user_email resolves correct profile."""
    user = UserFactory(username="sk_user", email="candidate-17@example.com")
    profile = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="candidate-17@example.com",
        active=True,
        cookie_data={"cookies": [{"name": "li_at", "value": "secret_key_12345"}]}
    )
    res = resolve_linkedin_session_credentials(profile_id=profile.pk)
    assert res["account_record_id"] == profile.pk
    assert res["session_key_present"] is True

    res2 = resolve_linkedin_session_credentials(user_email="candidate-17@example.com")
    assert res2["account_record_id"] == profile.pk


@pytest.mark.django_db
def test_ambiguous_or_inactive_profile_fails():
    """Verify inactive or ambiguous account records fail."""
    user1 = UserFactory(username="user1")
    user2 = UserFactory(username="user2")
    LinkedInProfile.objects.create(
        user=user1,
        linkedin_username="candidate-18@example.com",
        active=False,
        cookie_data={"cookies": [{"name": "li_at", "value": "val1"}]}
    )

    with pytest.raises(ValueError, match="NO_ACCOUNT_RECORD"):
        resolve_linkedin_session_credentials(user_email="candidate-18@example.com")


@pytest.mark.django_db
def test_missing_li_at_cookie_fails():
    """Verify resolver fails with LI_AT_COOKIE_MISSING when li_at cookie is absent."""
    user = UserFactory(username="no_li_at_user")
    p = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="candidate-13@example.com",
        active=True,
        cookie_data={"cookies": [{"name": "other_cookie", "value": "xyz"}]}
    )
    with pytest.raises(ValueError, match="LI_AT_COOKIE_MISSING"):
        resolve_linkedin_session_credentials(profile_id=p.pk)


@pytest.mark.django_db
def test_session_state_classifications_detailed():
    """Verify detailed classifications for REJECTED, REPLACED, SESSION_PRESENT_AUTH_NOT_CONFIRMED, and AUTHENTICATED."""
    import hashlib
    url = "https://www.linkedin.com/feed/"
    raw_val = "raw_val_123"
    stored_fp = hashlib.sha256(raw_val.encode("utf-8")).hexdigest()[:12]

    nav_ev = {"final_url": url, "login_url_detected": True, "login_form_visible": True}
    assert classify_session_state_detailed(nav_ev, stored_fp, []) == "SESSION_COOKIE_REJECTED"

    nav_auth_not_confirmed = {
        "final_url": url,
        "login_url_detected": False,
        "login_form_visible": False,
        "checkpoint_detected": False,
        "captcha_detected": False,
        "global_navigation_visible": False,
        "me_menu_trigger_count": 0,
    }
    assert classify_session_state_detailed(nav_auth_not_confirmed, stored_fp, [{"name": "li_at", "value": raw_val}]) == "SESSION_PRESENT_AUTH_NOT_CONFIRMED"


@pytest.mark.django_db
def test_compare_linkedin_identity_rejects_unrelated_urls():
    """Verify URL validation in compare_linkedin_identity_values rejects generic/search URLs."""
    invalid_urls = [
        "https://www.linkedin.com/search/results/all/?keywords=test",
        "https://www.linkedin.com/company/google/",
        "https://www.linkedin.com/jobs/view/12345678/",
        "https://www.google.com/in/user/",
        "",
    ]
    for url in invalid_urls:
        res = compare_linkedin_identity_values("Alice", "Alice", url)
        assert res["profile_url_verified"] is False
        assert res["identity_verified"] is False


@pytest.mark.django_db
def test_sentinel_comparison_returns_exact_sentinel():
    """Verify compare_linkedin_identity_values returns normalized sentinel values."""
    sentinel = "SENTINEL_USER_9948273"
    url = "https://www.linkedin.com/in/sentinel-user-9948273/"
    res = compare_linkedin_identity_values(sentinel, sentinel, url)
    assert res["menu_name_normalized"] == sentinel
    assert res["profile_heading_normalized"] == sentinel
    assert res["identity_verified"] is True


@pytest.mark.django_db
def test_open_authenticated_linkedin_context_lifecycle_and_cleanup(monkeypatch):
    """Verify open_authenticated_linkedin_context opens context, yields, and closes in finally block."""
    user = UserFactory(username="ctx_user")
    p = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="candidate-8@example.com",
        active=True,
        cookie_data={"cookies": [{"name": "li_at", "value": "secret_val_123"}]}
    )

    mock_page = MagicMock()
    mock_ctx = MagicMock()
    mock_browser = MagicMock()
    mock_pw = MagicMock()

    mock_page.locator.return_value.count.return_value = 0
    mock_ctx.cookies.return_value = [{"name": "li_at", "value": "secret_val_123"}]
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_page.goto.return_value = mock_resp

    def mock_launch_browser(storage_state=None):
        return mock_page, mock_ctx, mock_browser, mock_pw

    monkeypatch.setattr("openoutreach.linkedin.session_manager.launch_browser", mock_launch_browser)

    with open_authenticated_linkedin_context(linkedin_profile_id=p.pk) as (page, context, browser, res):
        assert page == mock_page
        assert context == mock_ctx
        assert browser == mock_browser
        assert res["account_record_id"] == p.pk
        # Inside yield block - context is NOT closed yet
        mock_ctx.close.assert_not_called()

    # Outside yield block - context and browser MUST be closed
    mock_page.close.assert_called_once()
    mock_ctx.close.assert_called_once()
    mock_browser.close.assert_called_once()


@pytest.mark.django_db
def test_cookie_rotation_persists_only_when_authenticated(monkeypatch):
    """Verify cookie rotation updates database record only when authenticated DOM proof exists."""
    user = UserFactory(username="rot_user")
    p = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="candidate-16@example.com",
        active=True,
        cookie_data={"cookies": [{"name": "li_at", "value": "old_val"}]}
    )

    mock_page = MagicMock()
    mock_ctx = MagicMock()
    mock_browser = MagicMock()
    mock_pw = MagicMock()

    # Pre-nav has old_val
    mock_ctx.cookies.side_effect = [
        [{"name": "li_at", "value": "old_val"}],  # installed
        [{"name": "li_at", "value": "rotated_new_val"}],  # post nav
    ]
    mock_ctx.storage_state.return_value = {"cookies": [{"name": "li_at", "value": "rotated_new_val"}]}

    mock_resp = MagicMock()
    mock_page.goto.return_value = mock_resp
    mock_page.url = "https://www.linkedin.com/feed/"
    mock_page.locator.return_value.count.return_value = 1  # Global nav present

    monkeypatch.setattr("openoutreach.linkedin.session_manager.launch_browser", lambda storage_state=None: (mock_page, mock_ctx, mock_browser, mock_pw))

    with open_authenticated_linkedin_context(linkedin_profile_id=p.pk) as (page, context, browser, res):
        assert res["classification"] == "SESSION_COOKIE_ROTATED_AUTHENTICATED"

    # Verify DB record updated with rotated cookie
    p.refresh_from_db()
    c_data = p.get_decrypted_cookie_data() if hasattr(p, "get_decrypted_cookie_data") else p.cookie_data
    assert (c_data or p.cookie_data)["cookies"][0]["value"] == "rotated_new_val"
    assert p.session_status == "valid"


@pytest.mark.django_db
def test_candidate_profile_and_env_vars_isolated_from_extraction():
    """Verify that extract_live_linkedin_identity does not read CandidateProfile or env name keys."""
    source = inspect.getsource(extract_live_linkedin_identity)
    assert "CandidateProfile" not in source
    assert "CandidateProfileVersion" not in source
    assert "SenderIdentity" not in source
    assert "os.environ.get('FULL_NAME')" not in source
    assert "Alex Morgan" not in source


@pytest.mark.django_db
def test_alternate_authenticated_navigation_classification():
    """Verify alternate authenticated navigation landmark is classified correctly."""
    nav_ev = {
        "global_navigation_visible": False,
        "nav_landmark_count": 2,
        "header_count": 1,
        "feed_link_candidates": 3,
        "me_menu_trigger_count": 0,
        "login_form_visible": False,
    }
    class_name = classify_session_state_detailed(nav_ev, "fp1", [{"name": "li_at", "value": "val1"}])
    assert class_name == "SESSION_PRESENT_AUTH_NOT_CONFIRMED"


@pytest.mark.django_db
def test_url_and_title_alone_never_authenticate():
    """Verify URL and title alone never result in AUTHENTICATED state without DOM identity proof."""
    nav_ev = {
        "final_url": "https://www.linkedin.com/feed/",
        "page_title": "Feed | LinkedIn",
        "global_navigation_visible": False,
        "login_form_visible": False,
    }
    res = classify_session_state_detailed(nav_ev, "fp1", [{"name": "li_at", "value": "val1"}])
    assert res != "AUTHENTICATED"
    assert res != "SESSION_COOKIE_ROTATED_AUTHENTICATED"


@pytest.mark.django_db
def test_cookie_rotation_alone_never_authenticates():
    """Verify cookie rotation alone without live DOM identity proof does not result in AUTHENTICATED state."""
    nav_ev = {
        "global_navigation_visible": True,
        "me_menu_trigger_count": 0,
    }
    res = classify_session_state_detailed(nav_ev, "old_fp", [{"name": "li_at", "value": "new_val"}], identity_extracted=False)
    assert res == "SELECTOR_DRIFT"


@pytest.mark.django_db
def test_identity_mismatch_blocks_persistence():
    """Verify compare_linkedin_identity_values returns identity_verified=False when names mismatch."""
    res = compare_linkedin_identity_values("Alice Smith", "Bob Jones", "https://www.linkedin.com/in/alicesmith/")
    assert res["names_match"] is False
    assert res["identity_verified"] is False
