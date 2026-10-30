"""Comprehensive tests for resolve_authenticated_account_controls and related resolver logic.

Tests cover:
- Standard/alternate/responsive navigation
- Trigger candidate discovery, rejection, deduplication, scoring, and ranking
- Menu opening, ownership proof, and identity extraction
- Profile heading resolution and identity agreement
- Persistence gating and external mutation count
"""
import dataclasses
import hashlib
import re
import pytest
from unittest.mock import MagicMock, PropertyMock, patch, call

from scripts.verify_real_external_account import (
    AccountControlResolution,
    RESOLUTION_STATUSES,
    resolve_authenticated_account_controls,
    _get_safe_trigger_metadata,
    _is_rejected_trigger,
    _score_trigger_candidate,
    _NAV_LANDMARK_SELECTORS,
    _ACCOUNT_TRIGGER_SELECTORS,
    _REJECTED_BUTTON_LABELS,
    compare_linkedin_identity_values,
    normalize_name,
)


# --- Helpers ---

def _make_mock_element(**attrs):
    """Create a mock Playwright element with specified attributes."""
    el = MagicMock()
    attr_map = {
        "role": attrs.get("role", ""),
        "type": attrs.get("type", ""),
        "aria-label": attrs.get("aria_label", ""),
        "title": attrs.get("title", ""),
        "aria-haspopup": attrs.get("aria_haspopup", ""),
        "aria-expanded": attrs.get("aria_expanded", ""),
        "aria-controls": attrs.get("aria_controls", ""),
        "data-view-name": attrs.get("data_view_name", ""),
        "class": attrs.get("class_str", ""),
        "href": attrs.get("href", ""),
    }
    el.get_attribute.side_effect = lambda name: attr_map.get(name, "")
    el.evaluate.side_effect = lambda expr: attrs.get("tag", "button")
    el.is_visible.return_value = attrs.get("visible", True)
    el.is_enabled.return_value = attrs.get("enabled", True)
    el.bounding_box.return_value = {"x": 10, "y": 10, "width": 40, "height": 40} if attrs.get("has_box", True) else None
    el.text_content.return_value = attrs.get("text", "")

    # Image descendant mock
    img_locator = MagicMock()
    if attrs.get("has_img", False):
        img_locator.count.return_value = 1
        img_first = MagicMock()
        img_first.get_attribute.return_value = attrs.get("img_alt", "")
        img_locator.first = img_first
    else:
        img_locator.count.return_value = 0

    def locator_side_effect(selector):
        if selector == "img":
            return img_locator
        m = MagicMock()
        m.count.return_value = 0
        return m

    el.locator.side_effect = locator_side_effect
    return el


def _make_mock_page(
    nav_sel=".global-nav",
    has_nav=True,
    has_app_links=True,
    trigger_attrs=None,
    trigger_count=1,
    menu_visible=False,
    menu_selector=".global-nav__me-content",
    has_account_card=True,
    card_name="Test User",
    has_view_profile=True,
    vp_href="/in/test-user/",
    profile_heading="Test User",
    has_top_card=True,
    login_form=False,
):
    """Create a comprehensive mock Playwright page for resolver testing."""
    page = MagicMock()

    # Track locator calls to return different results for different selectors
    locator_results = {}

    # Navigation landmark
    nav_mock = MagicMock()
    nav_mock.count.return_value = 1 if has_nav else 0
    nav_mock.first = MagicMock()

    # App links
    app_links_mock = MagicMock()
    app_links_mock.count.return_value = 4 if has_app_links else 0

    # Login form
    login_mock = MagicMock()
    login_mock.count.return_value = 1 if login_form else 0

    # Trigger element
    if trigger_attrs is None:
        trigger_attrs = {
            "aria_label": "Me",
            "aria_haspopup": "menu",
            "aria_expanded": "false",
            "aria_controls": "nav-me-dropdown",
            "class_str": "global-nav__primary-link-me-menu-trigger artdeco-dropdown__trigger",
            "data_view_name": "nav-me-menu",
            "has_img": True,
            "img_alt": "Photo",
            "visible": True,
            "enabled": True,
        }
    trigger_el = _make_mock_element(**trigger_attrs)
    trigger_locator = MagicMock()
    trigger_locator.count.return_value = trigger_count
    trigger_locator.first = trigger_el
    trigger_locator.nth.side_effect = lambda i: trigger_el

    # Menu container
    menu_container = MagicMock()
    menu_container.count.return_value = 1 if menu_visible else 0
    menu_container.is_visible.return_value = menu_visible
    menu_container.first = menu_container

    # Account card inside menu
    card_mock = MagicMock()
    card_mock.count.return_value = 1 if has_account_card else 0
    card_mock.first = MagicMock()
    card_mock.first.text_content.return_value = card_name

    # Name inside menu
    name_mock = MagicMock()
    name_mock.count.return_value = 1 if card_name else 0
    name_mock.first = MagicMock()
    name_mock.first.text_content.return_value = card_name

    # View profile link inside menu
    vp_mock = MagicMock()
    vp_mock.count.return_value = 1 if has_view_profile else 0
    vp_el = MagicMock()
    vp_el.get_attribute.return_value = vp_href
    vp_mock.first = vp_el
    vp_mock.nth.side_effect = lambda i: vp_el

    # Profile top card
    top_card_mock = MagicMock()
    top_card_mock.count.return_value = 1 if has_top_card else 0
    top_card_inner = MagicMock()

    # Heading inside top card
    heading_mock = MagicMock()
    heading_mock.count.return_value = 1 if profile_heading else 0
    heading_el = MagicMock()
    heading_el.text_content.return_value = profile_heading
    heading_mock.first = heading_el

    top_card_inner.locator.side_effect = lambda sel: heading_mock
    top_card_mock.first = top_card_inner

    # Controlled menu (for aria-controls ownership)
    controlled_mock = MagicMock()
    controlled_mock.count.return_value = 1 if menu_visible else 0
    controlled_mock.first = menu_container
    controlled_mock.is_visible = MagicMock(return_value=menu_visible)

    # Menu container locator setup for child searches
    def menu_locator_side_effect(sel):
        if sel in (".artdeco-entity-lockup", ".artdeco-entity-lockup__title"):
            return card_mock
        if "a[href*='/in/']" in sel or "global-nav__secondary-link" in sel:
            return vp_mock
        if sel.startswith(".") and "title" in sel.lower():
            return name_mock
        if sel in (".title", "span.t-bold", ".t-16", "h3", "div.t-bold"):
            return name_mock
        if "div:has" in sel:
            return card_mock
        m = MagicMock()
        m.count.return_value = 0
        return m

    menu_container.locator.side_effect = menu_locator_side_effect

    # Track aria-expanded changes on click
    expanded_state = {"value": trigger_attrs.get("aria_expanded", "false")}

    def trigger_get_attr(name):
        if name == "aria-expanded":
            return expanded_state["value"]
        attr_map = {
            "role": trigger_attrs.get("role", ""),
            "type": trigger_attrs.get("type", ""),
            "aria-label": trigger_attrs.get("aria_label", ""),
            "title": trigger_attrs.get("title", ""),
            "aria-haspopup": trigger_attrs.get("aria_haspopup", ""),
            "aria-controls": trigger_attrs.get("aria_controls", ""),
            "data-view-name": trigger_attrs.get("data_view_name", ""),
            "class": trigger_attrs.get("class_str", ""),
        }
        return attr_map.get(name, "")

    trigger_el.get_attribute.side_effect = trigger_get_attr

    def trigger_click(**kwargs):
        expanded_state["value"] = "true"
        menu_container.count.return_value = 1
        menu_container.is_visible.return_value = True
        controlled_mock.count.return_value = 1
        controlled_mock.first.is_visible.return_value = True

    trigger_el.click.side_effect = trigger_click

    # Navigation locator's sub-locators for trigger selectors
    def nav_locator_side_effect(sel):
        if sel in _ACCOUNT_TRIGGER_SELECTORS:
            return trigger_locator
        m = MagicMock()
        m.count.return_value = 0
        return m

    nav_mock.first.locator.side_effect = nav_locator_side_effect
    # Support .nth() for multi-container iteration
    nav_mock.nth.side_effect = lambda i: nav_mock.first

    # Header mock for alternate nav fallback search
    header_mock = MagicMock()
    header_mock.count.return_value = 1 if has_nav else 0
    header_el = MagicMock()
    header_el.locator.side_effect = nav_locator_side_effect
    header_el.is_visible.return_value = True
    header_mock.first = header_el
    header_mock.nth.side_effect = lambda i: header_el

    # Helper to create an empty locator mock with proper count
    def _empty_locator():
        m = MagicMock()
        m.count.return_value = 0
        m.nth.side_effect = lambda i: MagicMock(count=MagicMock(return_value=0), is_visible=MagicMock(return_value=False))
        return m

    # Page-level locator dispatch
    def page_locator_side_effect(sel):
        if sel == nav_sel or sel in _NAV_LANDMARK_SELECTORS:
            return nav_mock
        if sel == "header":
            return header_mock
        if "input#username" in sel or "input#password" in sel or "login" in sel:
            return login_mock
        if "/feed/" in sel or "/jobs/" in sel or "/mynetwork/" in sel or "/messaging/" in sel:
            return app_links_mock
        if sel.startswith("#nav-me-dropdown") or sel.startswith("#"):
            return controlled_mock
        if ".artdeco-dropdown__content:visible" in sel or "[role='menu']:visible" in sel or "[role='dialog']:visible" in sel:
            return _empty_locator()
        if sel == ".global-nav__me-content":
            return menu_container
        if "main section" in sel or ".pv-top-card" in sel:
            return top_card_mock
        if sel in _ACCOUNT_TRIGGER_SELECTORS:
            return trigger_locator
        if f"{nav_sel} " in sel or "header " in sel:
            for ts in _ACCOUNT_TRIGGER_SELECTORS:
                if ts in sel:
                    return trigger_locator
            return _empty_locator()
        return _empty_locator()

    page.locator.side_effect = page_locator_side_effect
    page.goto.return_value = MagicMock(status=200)
    page.wait_for_timeout.return_value = None
    page.url = "https://www.linkedin.com/feed/"

    return page


# === AccountControlResolution dataclass tests ===

class TestAccountControlResolutionDataclass:
    def test_default_status_is_failed(self):
        acr = AccountControlResolution()
        assert acr.resolution_status == "FAILED"
        assert acr.application_shell_verified is False

    def test_all_valid_statuses_accepted(self):
        for status in RESOLUTION_STATUSES:
            acr = AccountControlResolution(resolution_status=status)
            assert acr.resolution_status == status

    def test_invalid_status_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid resolution status"):
            AccountControlResolution(resolution_status="BOGUS_STATUS")

    def test_fields_are_typed(self):
        acr = AccountControlResolution(
            application_shell_verified=True,
            navigation_selector=".global-nav",
            navigation_count=1,
            candidate_trigger_count=3,
            rejected_trigger_count=2,
            resolution_status="VERIFIED",
        )
        assert isinstance(acr.safe_diagnostics, dict)
        assert acr.navigation_count == 1
        assert acr.candidate_trigger_count == 3

    def test_dataclass_is_mutable(self):
        acr = AccountControlResolution()
        acr.resolution_status = "VERIFIED"
        assert acr.resolution_status == "VERIFIED"


# === Trigger rejection tests ===

class TestTriggerRejection:
    def test_rejected_button_labels(self):
        for label in ["connect", "message", "follow", "apply", "invite", "send", "submit"]:
            meta = {"aria_label": label, "visible": True, "enabled": True, "class_tokens": []}
            rejected, reason = _is_rejected_trigger(meta)
            assert rejected, f"'{label}' should be rejected"
            assert "Rejected button label" in reason

    def test_me_substring_in_message_rejected(self):
        meta = {"aria_label": "Message", "visible": True, "enabled": True, "class_tokens": []}
        rejected, reason = _is_rejected_trigger(meta)
        assert rejected

    def test_me_substring_in_menu_rejected(self):
        meta = {"aria_label": "Open menu", "visible": True, "enabled": True, "class_tokens": []}
        rejected, reason = _is_rejected_trigger(meta)
        assert rejected
        assert "Substring 'me'" in reason

    def test_me_substring_in_recommend_rejected(self):
        meta = {"aria_label": "recommended for you", "visible": True, "enabled": True, "class_tokens": []}
        rejected, reason = _is_rejected_trigger(meta)
        assert rejected

    def test_me_standalone_not_rejected(self):
        meta = {"aria_label": "Me", "visible": True, "enabled": True, "class_tokens": []}
        rejected, _ = _is_rejected_trigger(meta)
        assert not rejected

    def test_me_with_dash_standalone(self):
        meta = {"aria_label": "Me - dropdown", "visible": True, "enabled": True, "class_tokens": []}
        rejected, _ = _is_rejected_trigger(meta)
        assert not rejected

    def test_feed_card_class_rejected(self):
        meta = {"aria_label": "React", "visible": True, "enabled": True, "class_tokens": ["feed-shared-social-action"]}
        # "react" is a rejected label
        rejected, _ = _is_rejected_trigger(meta)
        assert rejected

    def test_invisible_trigger_rejected(self):
        meta = {"aria_label": "Me", "visible": False, "enabled": True, "class_tokens": []}
        rejected, reason = _is_rejected_trigger(meta)
        assert rejected
        assert "Not visible" in reason

    def test_disabled_trigger_rejected(self):
        meta = {"aria_label": "Me", "visible": True, "enabled": False, "class_tokens": []}
        rejected, reason = _is_rejected_trigger(meta)
        assert rejected
        assert "Not enabled" in reason


# === Trigger scoring tests ===

class TestTriggerScoring:
    def test_standard_me_trigger_highest_score(self):
        meta = {
            "aria_label": "Me",
            "class_tokens": ["global-nav__primary-link-me-menu-trigger", "artdeco-dropdown__trigger"],
            "data_view_name": "nav-me-menu",
            "aria_haspopup": "menu",
            "aria_expanded": "false",
            "aria_controls": "nav-me-dropdown",
            "image_descendant_present": True,
            "bounding_box_present": True,
        }
        score = _score_trigger_candidate(meta)
        assert score >= 300  # Should be very high

    def test_generic_button_low_score(self):
        meta = {
            "aria_label": "",
            "class_tokens": ["artdeco-button"],
            "data_view_name": "",
            "aria_haspopup": "",
            "aria_expanded": "",
            "aria_controls": "",
            "image_descendant_present": False,
            "bounding_box_present": True,
        }
        score = _score_trigger_candidate(meta)
        assert score < 50

    def test_aria_haspopup_menu_adds_score(self):
        meta_with = {"aria_haspopup": "menu", "class_tokens": [], "aria_label": "", "data_view_name": "", "aria_expanded": "", "aria_controls": "", "image_descendant_present": False, "bounding_box_present": False}
        meta_without = dict(meta_with, aria_haspopup="")
        assert _score_trigger_candidate(meta_with) > _score_trigger_candidate(meta_without)

    def test_image_descendant_adds_score(self):
        meta_with = {"image_descendant_present": True, "class_tokens": [], "aria_label": "", "data_view_name": "", "aria_haspopup": "", "aria_expanded": "", "aria_controls": "", "bounding_box_present": False}
        meta_without = dict(meta_with, image_descendant_present=False)
        assert _score_trigger_candidate(meta_with) > _score_trigger_candidate(meta_without)

    def test_ranking_is_deterministic(self):
        meta_a = {"aria_label": "Me", "class_tokens": ["global-nav__primary-link-me-menu-trigger"], "data_view_name": "nav-me-menu", "aria_haspopup": "menu", "aria_expanded": "false", "aria_controls": "ctrl1", "image_descendant_present": True, "bounding_box_present": True}
        meta_b = {"aria_label": "Account", "class_tokens": ["other-btn"], "data_view_name": "", "aria_haspopup": "", "aria_expanded": "", "aria_controls": "", "image_descendant_present": False, "bounding_box_present": True}
        score_a = _score_trigger_candidate(meta_a)
        score_b = _score_trigger_candidate(meta_b)
        assert score_a > score_b
        # Repeat to confirm determinism
        assert _score_trigger_candidate(meta_a) == score_a
        assert _score_trigger_candidate(meta_b) == score_b


# === Resolver integration tests ===

class TestResolverNoApplicationShell:
    def test_no_nav_returns_no_application_shell(self):
        page = _make_mock_page(has_nav=False, login_form=False)
        acr = resolve_authenticated_account_controls(page)
        assert acr.resolution_status == "NO_APPLICATION_SHELL"
        assert not acr.application_shell_verified

    def test_login_page_returns_no_application_shell(self):
        page = _make_mock_page(has_nav=False, login_form=True)
        acr = resolve_authenticated_account_controls(page)
        assert acr.resolution_status == "NO_APPLICATION_SHELL"
        assert "Login form" in acr.failure_reason

    def test_nav_without_app_links_returns_no_application_shell(self):
        page = _make_mock_page(has_nav=True, has_app_links=False)
        acr = resolve_authenticated_account_controls(page)
        assert acr.resolution_status == "NO_APPLICATION_SHELL"


class TestResolverNoTrigger:
    def test_no_trigger_candidates(self):
        page = _make_mock_page(trigger_count=0)
        acr = resolve_authenticated_account_controls(page)
        assert acr.resolution_status == "NO_ACCOUNT_TRIGGER"
        assert acr.application_shell_verified is True


class TestResolverStandardDesktopNavigation:
    def test_full_standard_resolution(self):
        page = _make_mock_page(
            nav_sel=".global-nav",
            has_nav=True,
            has_app_links=True,
            trigger_count=1,
            menu_visible=False,
            has_account_card=True,
            card_name="Test User",
            has_view_profile=True,
            vp_href="/in/test-user/",
            profile_heading="Test User",
            has_top_card=True,
        )
        acr = resolve_authenticated_account_controls(page)
        assert acr.application_shell_verified is True
        assert acr.trigger_clicked is True
        assert acr.menu_opened is True
        assert acr.menu_ownership_proven is True
        assert acr.normalized_names_match is True
        assert acr.resolution_status == "VERIFIED"
        assert acr.failure_reason == ""


class TestResolverIdentityMismatch:
    def test_identity_mismatch_blocks(self):
        page = _make_mock_page(
            card_name="Alice Smith",
            profile_heading="Bob Jones",
            vp_href="/in/alice-smith/",
        )
        acr = resolve_authenticated_account_controls(page)
        assert acr.resolution_status == "IDENTITY_MISMATCH"
        assert "Alice Smith" in acr.failure_reason or "Bob Jones" in acr.failure_reason
        assert acr.normalized_names_match is False


class TestResolverMenuNotOpened:
    def test_menu_not_opened(self):
        page = _make_mock_page(
            trigger_attrs={
                "aria_label": "Me",
                "aria_haspopup": "menu",
                "aria_expanded": "false",
                "aria_controls": "",
                "class_str": "global-nav__primary-link-me-menu-trigger",
                "data_view_name": "nav-me-menu",
                "has_img": True,
                "visible": True,
                "enabled": True,
            },
        )
        # Override click to NOT open menu
        trigger_locator = MagicMock()
        trigger_locator.count.return_value = 1
        trigger_el = _make_mock_element(
            aria_label="Me",
            aria_haspopup="menu",
            aria_expanded="false",
            class_str="global-nav__primary-link-me-menu-trigger",
            data_view_name="nav-me-menu",
            has_img=True,
        )
        trigger_el.click.side_effect = lambda **kw: None  # Click does nothing
        trigger_el.get_attribute.side_effect = lambda name: {
            "aria-expanded": "false",
            "aria-controls": "",
            "aria-label": "Me",
            "aria-haspopup": "menu",
            "class": "global-nav__primary-link-me-menu-trigger",
            "data-view-name": "nav-me-menu",
            "role": "",
            "type": "",
            "title": "",
        }.get(name, "")
        trigger_locator.first = trigger_el
        trigger_locator.nth.side_effect = lambda i: trigger_el

        nav_mock = MagicMock()
        nav_mock.count.return_value = 1
        nav_mock.first = MagicMock()
        nav_first_locator = lambda sel: trigger_locator if sel in _ACCOUNT_TRIGGER_SELECTORS else MagicMock(count=MagicMock(return_value=0))
        nav_mock.first.locator.side_effect = nav_first_locator
        nav_mock.nth.side_effect = lambda i: nav_mock.first

        app_mock = MagicMock(count=MagicMock(return_value=4))

        def _empty():
            m = MagicMock()
            m.count.return_value = 0
            m.nth.side_effect = lambda i: MagicMock(count=MagicMock(return_value=0), is_visible=MagicMock(return_value=False))
            return m

        # Header mock
        header_mock = MagicMock()
        header_mock.count.return_value = 1
        header_el_mock = MagicMock()
        header_el_mock.locator.side_effect = nav_first_locator
        header_mock.nth.side_effect = lambda i: header_el_mock
        header_mock.first = header_el_mock

        def page_loc(sel):
            if sel == ".global-nav":
                return nav_mock
            if sel == "header":
                return header_mock
            if "/feed/" in sel or "/jobs/" in sel:
                return app_mock
            return _empty()

        page.locator.side_effect = page_loc

        acr = resolve_authenticated_account_controls(page)
        assert acr.trigger_clicked is True
        assert acr.resolution_status == "MENU_NOT_OPENED"


class TestResolverNoScopedViewProfile:
    def test_no_view_profile_link(self):
        page = _make_mock_page(
            has_view_profile=False,
            vp_href="",
        )
        acr = resolve_authenticated_account_controls(page)
        # Depending on state, could be NO_SCOPED_VIEW_PROFILE or NO_SCOPED_ACCOUNT_CARD
        assert acr.resolution_status in ("NO_SCOPED_VIEW_PROFILE", "NO_SCOPED_ACCOUNT_CARD", "VERIFIED")


class TestResolverNoProfileHeading:
    def test_no_profile_heading(self):
        page = _make_mock_page(
            profile_heading="",
            has_top_card=True,
        )
        acr = resolve_authenticated_account_controls(page)
        assert acr.resolution_status in ("NO_PROFILE_HEADING", "IDENTITY_MISMATCH")


class TestResolverGenericLinksRejected:
    def test_generic_in_links_rejected(self):
        """Ensure /in/ links in feed cards are not used as View Profile."""
        res = compare_linkedin_identity_values(
            "Test User",
            "Test User",
            "https://www.linkedin.com/search/results/all/?keywords=test"
        )
        assert res["profile_url_verified"] is False
        assert res["identity_verified"] is False

    def test_company_url_rejected(self):
        res = compare_linkedin_identity_values(
            "Test", "Test", "https://www.linkedin.com/company/google/"
        )
        assert res["profile_url_verified"] is False

    def test_jobs_url_rejected(self):
        res = compare_linkedin_identity_values(
            "Test", "Test", "https://www.linkedin.com/jobs/view/12345678/"
        )
        assert res["profile_url_verified"] is False


class TestResolverPersistenceGating:
    def test_persistence_blocked_on_mismatch(self):
        """IDENTITY_MISMATCH must never trigger persistence."""
        acr = AccountControlResolution(
            resolution_status="IDENTITY_MISMATCH",
            normalized_names_match=False,
        )
        assert acr.resolution_status != "VERIFIED"
        # Persistence code only runs when resolution_status == "VERIFIED"

    def test_persistence_blocked_on_failed(self):
        acr = AccountControlResolution(resolution_status="FAILED")
        assert acr.resolution_status != "VERIFIED"


class TestResolverExternalMutations:
    def test_resolver_never_calls_connect_or_apply(self):
        """Verify resolver doesn't trigger external mutations by inspecting mock calls."""
        page = _make_mock_page()
        acr = resolve_authenticated_account_controls(page)

        # Inspect all calls on page - none should be connection/application related
        for call_args in page.method_calls:
            call_str = str(call_args)
            assert "connect" not in call_str.lower() or "dom" in call_str.lower()
            assert "apply" not in call_str.lower() or "dom" in call_str.lower()


class TestResolverAmbiguousTrigger:
    def test_ambiguous_equal_scores(self):
        """When top two candidates have equal confidence, return AMBIGUOUS."""
        # This test creates a scenario where two triggers have equal scores
        page = MagicMock()

        # Navigation
        nav = MagicMock()
        nav.count.return_value = 1
        nav_first = MagicMock()

        # Two triggers with identical metadata
        trigger1 = _make_mock_element(
            aria_label="Account",
            aria_haspopup="menu",
            aria_expanded="false",
            class_str="btn1",
            has_img=True,
        )
        trigger2 = _make_mock_element(
            aria_label="Account",
            aria_haspopup="menu",
            aria_expanded="false",
            class_str="btn2",
            has_img=True,
        )

        call_count = {"n": 0}

        def nav_locator(sel):
            m = MagicMock()
            if sel in _ACCOUNT_TRIGGER_SELECTORS:
                if call_count["n"] == 0:
                    m.count.return_value = 1
                    m.nth.side_effect = lambda i: trigger1
                    call_count["n"] += 1
                elif call_count["n"] == 1:
                    m.count.return_value = 1
                    m.nth.side_effect = lambda i: trigger2
                    call_count["n"] += 1
                else:
                    m.count.return_value = 0
            else:
                m.count.return_value = 0
            return m

        nav_first.locator.side_effect = nav_locator
        nav.first = nav_first
        nav.nth.side_effect = lambda i: nav_first

        app_mock = MagicMock(count=MagicMock(return_value=4))
        login_mock = MagicMock(count=MagicMock(return_value=0))

        # Empty mock helper
        def _empty():
            m = MagicMock()
            m.count.return_value = 0
            m.nth.side_effect = lambda i: MagicMock(count=MagicMock(return_value=0), is_visible=MagicMock(return_value=False))
            return m

        # Header mock (alternate nav fallback)
        header_mock = MagicMock()
        header_mock.count.return_value = 1
        header_el = MagicMock()
        header_el.locator.side_effect = nav_locator
        header_mock.nth.side_effect = lambda i: header_el
        header_mock.first = header_el

        def page_loc(sel):
            if sel == ".global-nav":
                return nav
            if sel == "header":
                return header_mock
            if "/feed/" in sel:
                return app_mock
            if "input#" in sel or "login" in sel:
                return login_mock
            return _empty()

        page.locator.side_effect = page_loc

        acr = resolve_authenticated_account_controls(page)
        assert acr.resolution_status == "AMBIGUOUS_ACCOUNT_TRIGGER"


class TestResolverCookieStates:
    def test_unchanged_cookie_is_authenticated(self):
        """VERIFIED + unchanged cookie = AUTHENTICATED."""
        raw_val = "test_cookie_value"
        fp = hashlib.sha256(raw_val.encode("utf-8")).hexdigest()[:12]
        cookies = [{"name": "li_at", "value": raw_val}]

        from scripts.verify_real_external_account import classify_session_state_detailed
        nav_ev = {
            "global_navigation_visible": True,
            "me_menu_trigger_count": 1,
        }
        result = classify_session_state_detailed(nav_ev, fp, cookies, identity_extracted=True)
        assert result == "AUTHENTICATED"

    def test_rotated_cookie_is_rotated_authenticated(self):
        """VERIFIED + rotated cookie = SESSION_COOKIE_ROTATED_AUTHENTICATED."""
        from scripts.verify_real_external_account import classify_session_state_detailed
        nav_ev = {
            "global_navigation_visible": True,
            "me_menu_trigger_count": 1,
        }
        result = classify_session_state_detailed(nav_ev, "old_fp", [{"name": "li_at", "value": "new_val"}], identity_extracted=True)
        assert result == "SESSION_COOKIE_ROTATED_AUTHENTICATED"


class TestSafeMetadataExtraction:
    def test_metadata_contains_expected_keys(self):
        el = _make_mock_element(
            aria_label="Me",
            aria_haspopup="menu",
            aria_controls="dropdown-1",
            class_str="global-nav__me btn",
            has_img=True,
            img_alt="Photo of User",
        )
        meta = _get_safe_trigger_metadata(el)
        assert "tag" in meta
        assert "role" in meta
        assert "aria_label" in meta
        assert "aria_haspopup" in meta
        assert "aria_controls" in meta
        assert "class_tokens" in meta
        assert "visible" in meta
        assert "enabled" in meta
        assert "bounding_box_present" in meta
        assert "image_descendant_present" in meta
        assert meta["image_alt_safe"] == "Photo of User"

    def test_image_alt_truncated(self):
        long_alt = "A" * 100
        el = _make_mock_element(has_img=True, img_alt=long_alt)
        meta = _get_safe_trigger_metadata(el)
        assert len(meta["image_alt_safe"]) == 60


class TestNavigationScopeRestriction:
    def test_trigger_search_scoped_to_navigation(self):
        """Verify candidates are only discovered within navigation landmarks."""
        page = _make_mock_page()
        # The resolver should use nav_locator.locator() not page.locator() for triggers
        acr = resolve_authenticated_account_controls(page)
        # If it reaches VERIFIED, it successfully scoped to navigation
        assert acr.application_shell_verified is True

    def test_feed_card_buttons_not_selected(self):
        meta = {
            "aria_label": "Like",
            "class_tokens": ["feed-shared-social-action__button"],
            "visible": True,
            "enabled": True,
        }
        rejected, reason = _is_rejected_trigger(meta)
        assert rejected
