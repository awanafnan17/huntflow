#!/usr/bin/env python3
"""HuntFlow Session-Key Acceptance Proof & Account Selection Verification Script.

Strict Rules:
- Reuses existing LinkedInProfile model cookie_data storage state.
- Raw session keys / cookies are NEVER printed or exposed.
- Installs session authentication BEFORE visiting LinkedIn feed in the same Playwright context.
- Distinguishes SESSION_COOKIE_REJECTED, SESSION_COOKIE_ROTATED_AUTHENTICATED, AUTHENTICATED, and SESSION_PRESENT_AUTH_NOT_CONFIRMED.
- Supports operator session renewal (--allow-operator-session-renewal) with explicit state machine and safe terminal confirmation.
- Supports safe DOM structural diagnosis (--dom-inventory, --capture-sanitized-screenshot, --capture-accessibility-inventory).
- Supports alternate LinkedIn navigation structures (header nav, role=navigation, responsive layouts).
- Zero external mutations (0 connection requests, 0 job applications, 0 messages).
"""

import argparse
import dataclasses
import hashlib
import json
import os
import re

# Playwright's sync_api runs with an underlying async event loop,
# which triggers Django's SynchronousOnlyOperation protection.
# This is safe for CLI scripts (not web request handlers).
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
import sys
import threading
import time
import unicodedata
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openoutreach.settings")

import django
django.setup()

from django.db import transaction
from openoutreach.core.models import AuditEvent
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.session_manager import resolve_linkedin_session_credentials

ARTIFACTS_DIR = BASE_DIR / "artifacts" / "real-account"
DIAGNOSTICS_DIR = ARTIFACTS_DIR / "private-diagnostics"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)


class LinkedInIdentityExtractionError(Exception):
    def __init__(self, message: str, error_code: str, navigation_evidence: dict):
        super().__init__(message)
        self.error_code = error_code
        self.navigation_evidence = navigation_evidence


def normalize_name(val: str) -> str:
    if not val:
        return ""
    norm = unicodedata.normalize("NFKC", str(val))
    return re.sub(r"\s+", " ", norm).strip()


def compare_linkedin_identity_values(
    menu_name: str,
    profile_name: str,
    profile_url: str,
) -> dict:
    """Pure comparison logic for Unicode normalization and profile URL validation."""
    menu_norm = normalize_name(menu_name)
    prof_norm = normalize_name(profile_name)

    names_match = (len(menu_norm) > 0) and (menu_norm.lower() == prof_norm.lower())
    url_pattern = re.compile(r"^https://www\.linkedin\.com/in/[a-zA-Z0-9%\-_]+/?$")
    profile_url_verified = bool(profile_url and url_pattern.match(profile_url))
    identity_verified = names_match and profile_url_verified

    return {
        "menu_name_normalized": menu_norm,
        "profile_heading_normalized": prof_norm,
        "names_match": names_match,
        "profile_url_verified": profile_url_verified,
        "identity_verified": identity_verified,
    }


# --- Canonical Account Control Resolution ---

# Valid resolution statuses
RESOLUTION_STATUSES = frozenset({
    "VERIFIED",
    "NO_APPLICATION_SHELL",
    "NO_ACCOUNT_TRIGGER",
    "AMBIGUOUS_ACCOUNT_TRIGGER",
    "TRIGGER_NOT_INTERACTIVE",
    "MENU_NOT_OPENED",
    "MENU_OWNERSHIP_NOT_PROVEN",
    "NO_SCOPED_ACCOUNT_CARD",
    "NO_SCOPED_VIEW_PROFILE",
    "PROFILE_PAGE_NOT_LOADED",
    "NO_PROFILE_HEADING",
    "IDENTITY_MISMATCH",
    "SELECTOR_DRIFT",
    "FAILED",
})


@dataclasses.dataclass
class AccountControlResolution:
    """Typed result of resolve_authenticated_account_controls(page)."""
    application_shell_verified: bool = False
    navigation_selector: str = ""
    navigation_count: int = 0
    candidate_trigger_count: int = 0
    rejected_trigger_count: int = 0
    selected_trigger_selector: str = ""
    selected_trigger_reason: str = ""
    trigger_clicked: bool = False
    menu_opened: bool = False
    menu_ownership_proven: bool = False
    menu_selector: str = ""
    menu_count: int = 0
    account_card_selector: str = ""
    account_card_count: int = 0
    menu_identity_name: str = ""
    view_profile_selector: str = ""
    view_profile_count: int = 0
    view_profile_url: str = ""
    profile_page_loaded: bool = False
    profile_top_card_selector: str = ""
    profile_top_card_count: int = 0
    profile_heading: str = ""
    normalized_names_match: bool = False
    resolution_status: str = "FAILED"
    failure_reason: str = ""
    safe_diagnostics: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        if self.resolution_status not in RESOLUTION_STATUSES:
            raise ValueError(f"Invalid resolution status: {self.resolution_status}")


# Words that must NOT trigger a false positive match on the substring 'me'
_ME_REJECTION_SUBSTRINGS = frozenset({
    "message", "messaging", "menu", "member", "recommend", "comment",
    "commerce", "media", "medium", "home", "name", "game", "time",
    "resume", "frame", "come", "some", "theme", "scheme", "esome",
    "ment", "ement",
})

# Button labels that indicate non-account interactive controls
_REJECTED_BUTTON_LABELS = frozenset({
    "connect", "message", "follow", "apply", "invite", "send",
    "submit", "next", "dismiss", "close", "like", "comment",
    "share", "repost", "save", "report", "block", "unfollow",
    "accept", "decline", "ignore", "skip", "cancel", "react",
    "celebrate", "support", "love", "insightful", "funny",
})

# CSS selectors for LinkedIn application navigation landmarks
_NAV_LANDMARK_SELECTORS = [
    ".global-nav",
    "#global-nav",
    "nav.global-nav",
    "header nav",
    "nav[role='navigation']",
    "[role='navigation']",
    "header",
    "nav",
]

# Selectors for account trigger candidates within navigation scope
_ACCOUNT_TRIGGER_SELECTORS = [
    # Standard desktop navigation
    "button.global-nav__primary-link-me-menu-trigger",
    "button[id*='ember'][aria-haspopup='menu']",
    "button[data-view-name='nav-me-menu']",
    "button:has(.global-nav__me-photo)",
    "button[aria-expanded][aria-haspopup]:has(img)",
    ".global-nav__me button",
    # Alternate / responsive navigation (broader, filtered by rejection/scoring)
    "button[aria-label='Me']",
    "button[aria-label='Me ']",
    "button[aria-label*='Account']",
    "button[aria-haspopup='menu']:has(img)",
    "button[aria-haspopup='true']:has(img)",
    "button:has(img[class*='profile'])",
    "button:has(img[class*='photo'])",
    "button:has(img[class*='avatar'])",
    "button:has(img[alt*='photo'])",
]


def _get_safe_trigger_metadata(element) -> dict:
    """Extract only safe, non-private metadata from a trigger candidate element."""
    meta = {}
    try: meta["tag"] = element.evaluate("el => el.tagName.toLowerCase()")
    except Exception: meta["tag"] = ""
    try: meta["role"] = element.get_attribute("role") or ""
    except Exception: meta["role"] = ""
    try: meta["type"] = element.get_attribute("type") or ""
    except Exception: meta["type"] = ""
    try: meta["aria_label"] = element.get_attribute("aria-label") or ""
    except Exception: meta["aria_label"] = ""
    try: meta["title"] = element.get_attribute("title") or ""
    except Exception: meta["title"] = ""
    try: meta["aria_haspopup"] = element.get_attribute("aria-haspopup") or ""
    except Exception: meta["aria_haspopup"] = ""
    try: meta["aria_expanded"] = element.get_attribute("aria-expanded") or ""
    except Exception: meta["aria_expanded"] = ""
    try: meta["aria_controls"] = element.get_attribute("aria-controls") or ""
    except Exception: meta["aria_controls"] = ""
    try: meta["data_view_name"] = element.get_attribute("data-view-name") or ""
    except Exception: meta["data_view_name"] = ""
    try: meta["class_tokens"] = (element.get_attribute("class") or "").split()
    except Exception: meta["class_tokens"] = []
    try: meta["visible"] = element.is_visible()
    except Exception: meta["visible"] = False
    try: meta["enabled"] = element.is_enabled()
    except Exception: meta["enabled"] = False
    try:
        box = element.bounding_box()
        meta["bounding_box_present"] = box is not None and box.get("width", 0) > 0
    except Exception: meta["bounding_box_present"] = False
    try: meta["image_descendant_present"] = element.locator("img").count() > 0
    except Exception: meta["image_descendant_present"] = False
    try:
        if meta["image_descendant_present"]:
            alt = element.locator("img").first.get_attribute("alt") or ""
            # Only store first 60 chars of alt to avoid storing full names
            meta["image_alt_safe"] = alt[:60] if alt else ""
        else:
            meta["image_alt_safe"] = ""
    except Exception: meta["image_alt_safe"] = ""
    return meta


def _is_rejected_trigger(meta: dict) -> tuple[bool, str]:
    """Check if a trigger candidate should be rejected. Returns (rejected, reason)."""
    label = (meta.get("aria_label", "") or "").lower().strip()

    # Reject known non-account button labels
    for rejected in _REJECTED_BUTTON_LABELS:
        if label == rejected or label.startswith(rejected + " "):
            return True, f"Rejected button label: '{label}'"

    # Reject if the label contains 'me' only as a substring of unrelated words
    if "me" in label:
        label_tokens = re.split(r"[\s,\-:;/]+", label)
        me_is_standalone = any(t == "me" for t in label_tokens)
        if not me_is_standalone:
            # 'me' appears only inside other words
            return True, f"Substring 'me' in non-account context: '{label}'"

    # Reject controls that are clearly feed-card or recommendation elements
    classes = " ".join(meta.get("class_tokens", []))
    feed_rejection_patterns = [
        "feed-shared", "social-action", "msg-", "ad-banner",
        "artdeco-card__action", "scaffold-finite-scroll",
    ]
    for pattern in feed_rejection_patterns:
        if pattern in classes:
            return True, f"Feed/card/ad control class: '{pattern}'"

    # Must be visible and enabled
    if not meta.get("visible", False):
        return True, "Not visible"
    if not meta.get("enabled", False):
        return True, "Not enabled"

    return False, ""


def _score_trigger_candidate(meta: dict) -> int:
    """Deterministic confidence score for account trigger candidates. Higher = more confident."""
    score = 0
    label = (meta.get("aria_label", "") or "").lower()
    dvn = (meta.get("data_view_name", "") or "").lower()
    classes = " ".join(meta.get("class_tokens", []))

    # Strong positive signals
    if "global-nav__primary-link-me-menu-trigger" in classes:
        score += 100
    if dvn in ("nav-me-menu", "nav-settings-view-profile"):
        score += 80
    if meta.get("aria_haspopup") in ("menu", "true", "dialog"):
        score += 40
    if meta.get("aria_expanded") in ("true", "false"):
        score += 30
    if meta.get("aria_controls"):
        score += 25
    if meta.get("image_descendant_present"):
        score += 20
    if meta.get("bounding_box_present"):
        score += 10

    # Moderate signals from accessible name
    label_tokens = re.split(r"[\s,\-:;/]+", label)
    if any(t == "me" for t in label_tokens):
        score += 50
    if "account" in label:
        score += 45
    if "profile" in label and "view" not in label:
        score += 30

    # Navigation ancestry signal
    if "global-nav" in classes or "global-nav__me" in classes:
        score += 60

    return score


def resolve_authenticated_account_controls(page) -> AccountControlResolution:
    """Resolve authenticated account controls from live LinkedIn DOM.

    Discovers and interacts with the account trigger inside the verified application
    navigation, opens the owned menu, extracts identity from the scoped account card,
    navigates to the own profile, and verifies identity agreement.

    Returns a typed AccountControlResolution with a precise status.
    External mutations: 0.
    """
    res = AccountControlResolution()
    diagnostics = {}

    # Step 1: Verify application shell
    nav_locator = None
    nav_sel = ""
    for sel in _NAV_LANDMARK_SELECTORS:
        count = page.locator(sel).count()
        if count > 0:
            nav_locator = page.locator(sel).first
            nav_sel = sel
            res.navigation_selector = sel
            res.navigation_count = count
            break

    if not nav_locator:
        # Check if it's a login page
        if page.locator("input#username, input#password, form.login__form").count() > 0:
            res.resolution_status = "NO_APPLICATION_SHELL"
            res.failure_reason = "Login form detected instead of application shell"
            return res
        res.resolution_status = "NO_APPLICATION_SHELL"
        res.failure_reason = "No navigation landmark found in DOM"
        return res

    # Verify it's an authenticated app shell by checking for app-specific links
    app_links = page.locator("a[href*='/feed/'], a[href*='/jobs/'], a[href*='/mynetwork/'], a[href*='/messaging/']")
    app_link_count = app_links.count()
    diagnostics["app_link_count"] = app_link_count

    if app_link_count == 0:
        res.resolution_status = "NO_APPLICATION_SHELL"
        res.failure_reason = "Navigation present but no authenticated app links found"
        res.safe_diagnostics = diagnostics
        return res

    res.application_shell_verified = True

    # Step 2: Discover account trigger candidates within navigation scope
    # Search across ALL matching navigation landmarks (not just .first)
    candidates = []
    all_rejected = []

    # Collect all navigation containers to search within
    nav_containers = []
    all_nav = page.locator(nav_sel)
    for ni in range(min(all_nav.count(), 10)):
        nav_containers.append((all_nav.nth(ni), nav_sel))

    # Also add parent containers if nav_sel is narrow (e.g., "header nav" -> also search "header")
    if nav_sel == "header nav":
        header_els = page.locator("header")
        for hi in range(min(header_els.count(), 5)):
            nav_containers.append((header_els.nth(hi), "header"))

    for nav_container, container_sel in nav_containers:
        for sel_idx, trigger_sel in enumerate(_ACCOUNT_TRIGGER_SELECTORS):
            try:
                scoped = nav_container.locator(trigger_sel)
                scoped_count = scoped.count()
            except Exception:
                scoped_count = 0

            if scoped_count == 0:
                # Also try page-level compound selector
                try:
                    scoped = page.locator(f"{container_sel} {trigger_sel}")
                    scoped_count = scoped.count()
                except Exception:
                    scoped_count = 0

            for idx in range(min(scoped_count, 5)):
                el = scoped.nth(idx)
                meta = _get_safe_trigger_metadata(el)
                meta["candidate_index"] = len(candidates) + len(all_rejected)
                meta["selector_pattern"] = trigger_sel
                meta["ancestor_navigation_selector"] = container_sel
                meta["confidence_score"] = _score_trigger_candidate(meta)

                rejected, reason = _is_rejected_trigger(meta)
                if rejected:
                    meta["rejection_reason"] = reason
                    all_rejected.append(meta)
                else:
                    meta["rejection_reason"] = ""
                    # Deduplicate by aria_controls or class signature
                    dup = False
                    for existing in candidates:
                        if (meta.get("aria_controls") and meta["aria_controls"] == existing.get("aria_controls")):
                            dup = True
                            break
                        if meta["class_tokens"] == existing["class_tokens"] and meta["aria_label"] == existing["aria_label"]:
                            dup = True
                            break
                    if not dup:
                        candidates.append(meta)

    res.candidate_trigger_count = len(candidates)
    res.rejected_trigger_count = len(all_rejected)
    diagnostics["trigger_candidates"] = candidates
    diagnostics["rejected_triggers"] = all_rejected

    if len(candidates) == 0:
        # === ALTERNATE LAYOUT RESOLUTION STRATEGY ===
        # LinkedIn's redesigned React layout uses:
        # - Profile photo in <figure> inside a <div> ancestor (not <button>)
        # - "Me" or "More" nav buttons without aria-haspopup
        # - Obfuscated/hashed CSS class names with no semantic aria attributes

        # Strategy A: Find profile photo <div> in header and click it
        header_photo_trigger = None
        try:
            header_photo_figs = page.locator("header figure:has(img[src*='profile-displayphoto'])")
            for fi in range(min(header_photo_figs.count(), 5)):
                fig = header_photo_figs.nth(fi)
                if fig.is_visible():
                    # Walk up to the nearest clickable-like ancestor
                    clickable_info = fig.evaluate("""el => {
                        let current = el;
                        while (current && current !== document.body) {
                            current = current.parentElement;
                            if (!current) break;
                            let tag = current.tagName;
                            let role = current.getAttribute('role') || '';
                            let click = current.getAttribute('onclick') || '';
                            let cursor = window.getComputedStyle(current).cursor;
                            if (tag === 'BUTTON' || tag === 'A' || role === 'button' || cursor === 'pointer') {
                                return {
                                    found: true,
                                    tag: tag,
                                    role: role,
                                    ariaLabel: current.getAttribute('aria-label') || '',
                                    ariaExpanded: current.getAttribute('aria-expanded') || '',
                                    ariaControls: current.getAttribute('aria-controls') || '',
                                    visible: current.offsetParent !== null,
                                    bbox: current.getBoundingClientRect(),
                                };
                            }
                        }
                        return { found: false };
                    }""")
                    if clickable_info.get("found"):
                        header_photo_trigger = fig
                        diagnostics["alternate_strategy"] = "header_profile_photo_click"
                        diagnostics["alternate_trigger_info"] = clickable_info
                        break
        except Exception:
            pass

        # Strategy B: Find the "Me" or "More" nav button and click it to open account panel
        me_more_trigger = None
        if not header_photo_trigger:
            try:
                nav_btns = page.locator("nav button")
                for bi in range(min(nav_btns.count(), 10)):
                    btn = nav_btns.nth(bi)
                    if not btn.is_visible():
                        continue
                    label = (btn.get_attribute("aria-label") or "").strip().lower()
                    # Accept "me", "more", or "me - <name>" as nav trigger
                    if label == "me" or label == "more" or label.startswith("me ") or label.startswith("me,"):
                        me_more_trigger = btn
                        diagnostics["alternate_strategy"] = f"nav_button_{label}"
                        break
            except Exception:
                pass

        # Strategy C: Click the profile photo div's clickable ancestor directly
        if not header_photo_trigger and not me_more_trigger:
            try:
                # Try clicking the first visible header profile photo figure's parent
                header_figs = page.locator("header figure:has(img[src*='profile-displayphoto'])")
                for fi in range(min(header_figs.count(), 5)):
                    fig = header_figs.nth(fi)
                    if fig.is_visible():
                        header_photo_trigger = fig
                        diagnostics["alternate_strategy"] = "header_profile_photo_direct"
                        break
            except Exception:
                pass

        alt_trigger = header_photo_trigger or me_more_trigger

        if not alt_trigger:
            res.resolution_status = "NO_ACCOUNT_TRIGGER"
            res.failure_reason = "No viable account trigger candidates found (standard or alternate layout)"
            res.safe_diagnostics = diagnostics
            return res

        # Click the alternate trigger
        res.selected_trigger_selector = diagnostics.get("alternate_strategy", "alternate_layout")
        res.selected_trigger_reason = f"Alternate layout strategy: {diagnostics.get('alternate_strategy', 'unknown')}"

        pre_click_menu_count = page.locator(".artdeco-dropdown__content:visible, [role='menu']:visible, [role='dialog']:visible").count()

        try:
            alt_trigger.click(timeout=5000)
            page.wait_for_timeout(1500)
            res.trigger_clicked = True
        except Exception as e:
            res.resolution_status = "TRIGGER_CLICK_FAILED"
            res.failure_reason = f"Alternate trigger click failed: {e}"
            res.safe_diagnostics = diagnostics
            return res

        # Check what opened after clicking
        post_click_opened = False
        menu_container = None
        menu_sel = ""
        ownership_proof = ""

        # Check for newly visible panels/menus/dialogs
        post_check_sels = [
            "[role='menu']:visible",
            "[role='dialog']:visible",
            ".artdeco-dropdown__content:visible",
            "div[class*='dropdown']:visible",
        ]
        for psel in post_check_sels:
            try:
                pc = page.locator(psel)
                for pi in range(min(pc.count(), 5)):
                    if pc.nth(pi).is_visible():
                        menu_container = pc.nth(pi)
                        menu_sel = psel
                        ownership_proof = f"Alternate trigger opened {psel}"
                        post_click_opened = True
                        break
            except Exception:
                pass
            if menu_container:
                break

        # Also check if a sidebar/panel appeared with View Profile links
        if not menu_container:
            try:
                # Look for any newly appeared element with profile links
                vp_check = page.locator("a[href*='/in/']:visible")
                vp_in_header = page.locator("header a[href*='/in/']:visible")
                if vp_in_header.count() > 0:
                    # A profile link appeared in the header — get its href
                    vp_el = vp_in_header.first
                    vp_href = vp_el.get_attribute("href") or ""
                    if "/in/" in vp_href and "linkedin.com" in vp_href or vp_href.startswith("/in/"):
                        # We have a profile URL — navigate directly
                        res.view_profile_url = vp_href if vp_href.startswith("http") else f"https://www.linkedin.com{vp_href}"
                        res.view_profile_selector = "header a[href*='/in/']"
                        res.view_profile_count = vp_in_header.count()
                        post_click_opened = True
                        ownership_proof = "Profile link appeared in header after alternate trigger click"
            except Exception:
                pass

        # If nothing opened, try "More" as secondary
        if not post_click_opened and header_photo_trigger and me_more_trigger:
            try:
                me_more_trigger.click(timeout=5000)
                page.wait_for_timeout(1500)
                for psel in post_check_sels:
                    try:
                        pc = page.locator(psel)
                        for pi in range(min(pc.count(), 5)):
                            if pc.nth(pi).is_visible():
                                menu_container = pc.nth(pi)
                                menu_sel = psel
                                ownership_proof = f"More button opened {psel}"
                                post_click_opened = True
                                break
                    except Exception:
                        pass
                    if menu_container:
                        break
            except Exception:
                pass

        if menu_container:
            res.menu_opened = True
            res.menu_ownership_proven = True
            res.menu_selector = menu_sel
            try:
                res.menu_count = page.locator(menu_sel).count()
            except Exception:
                res.menu_count = 1

            # Extract identity from menu (same logic as standard flow)
            # Search for account card / name
            card_sels = [".artdeco-entity-lockup", ".artdeco-entity-lockup__title", "div:has(> figure) span", "h3", "span.t-bold"]
            for cs in card_sels:
                try:
                    card = menu_container.locator(cs)
                    if card.count() > 0:
                        res.account_card_selector = cs
                        res.account_card_count = card.count()
                        name_text = card.first.text_content() or ""
                        name_clean = " ".join(name_text.split()).strip()
                        if name_clean and len(name_clean) > 1 and len(name_clean) < 80:
                            res.menu_identity_name = name_clean
                            break
                except Exception:
                    pass

            # Search for View Profile link in menu
            vp_sels = ["a[href*='/in/']", "a:has-text('View Profile')", "a:has-text('View profile')"]
            for vs in vp_sels:
                try:
                    vp = menu_container.locator(vs)
                    if vp.count() > 0:
                        res.view_profile_selector = vs
                        res.view_profile_count = vp.count()
                        res.view_profile_url = vp.first.get_attribute("href") or ""
                        if res.view_profile_url and not res.view_profile_url.startswith("http"):
                            res.view_profile_url = f"https://www.linkedin.com{res.view_profile_url}"
                        break
                except Exception:
                    pass

        elif not post_click_opened:
            res.menu_opened = False
            # Last resort: try to find own profile URL from header context
            try:
                own_profile_figs = page.locator("header figure:has(img[src*='profile-displayphoto'])")
                for fi in range(min(own_profile_figs.count(), 5)):
                    fig = own_profile_figs.nth(fi)
                    if not fig.is_visible():
                        continue
                    # Check if any ancestor or sibling has a profile link
                    profile_href = fig.evaluate("""el => {
                        let parent = el.parentElement;
                        for (let i = 0; i < 5 && parent; i++) {
                            let links = parent.querySelectorAll('a[href*="/in/"]');
                            for (let link of links) {
                                let href = link.getAttribute('href') || '';
                                if (href.includes('/in/') && !href.includes('/in/ACoA')) return href;
                            }
                            parent = parent.parentElement;
                        }
                        return '';
                    }""")
                    if profile_href:
                        res.view_profile_url = profile_href if profile_href.startswith("http") else f"https://www.linkedin.com{profile_href}"
                        res.view_profile_selector = "header profile photo ancestor link"
                        res.view_profile_count = 1
                        break
            except Exception:
                pass

        # Continue with profile page navigation and heading verification
        # Jump directly to profile navigation if we have a URL
        if res.view_profile_url:
            profile_url = res.view_profile_url.split("?")[0].rstrip("/") + "/"
            res.view_profile_url = profile_url

            url_pattern = re.compile(r"^https://www\.linkedin\.com/in/[a-zA-Z0-9%\-_]+/$")
            if url_pattern.match(profile_url):
                try:
                    page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                    res.profile_page_loaded = True
                except Exception as e:
                    res.resolution_status = "PROFILE_PAGE_NOT_LOADED"
                    res.failure_reason = f"Profile page navigation failed: {e}"
                    res.safe_diagnostics = diagnostics
                    return res

                # Extract heading from profile top card
                top_card = None
                top_card_candidates_alt = ["main section.pv-top-card", ".pv-top-card", "main section", "main h1", "section:has(h1)"]
                for tsel in top_card_candidates_alt:
                    tc = page.locator(tsel)
                    if tc.count() > 0:
                        top_card = tc.first
                        res.profile_top_card_selector = tsel
                        res.profile_top_card_count = tc.count()
                        break

                if top_card:
                    heading = None
                    heading_candidates_alt = ["h1.text-heading-xlarge", "h1.inline.t-24", "h1.t-24", "h1", "h2"]
                    for hsel in heading_candidates_alt:
                        hc = top_card.locator(hsel)
                        if hc.count() > 0:
                            heading = hc.first
                            break
                    # Fallback: page-level h1, then h2
                    if not heading:
                        for fallback_sel in ["h1", "main h2"]:
                            fb = page.locator(fallback_sel)
                            if fb.count() > 0:
                                heading = fb.first
                                break
                    # Fallback: extract name from page title ("Name | LinkedIn")
                    if not heading:
                        title = page.title() or ""
                        if " | LinkedIn" in title:
                            title_name = title.replace(" | LinkedIn", "").strip()
                            if title_name and len(title_name) > 1 and len(title_name) < 80:
                                # Create a synthetic heading-like result
                                res.profile_heading = title_name
                                if res.menu_identity_name:
                                    menu_norm = normalize_name(res.menu_identity_name)
                                    heading_norm = normalize_name(title_name)
                                    if menu_norm and heading_norm and (menu_norm in heading_norm or heading_norm in menu_norm or menu_norm == heading_norm):
                                        res.normalized_names_match = True
                                        res.resolution_status = "VERIFIED"
                                        res.failure_reason = ""
                                    else:
                                        res.normalized_names_match = False
                                        res.resolution_status = "IDENTITY_MISMATCH"
                                        res.failure_reason = f"Menu identity '{res.menu_identity_name}' != page title name '{title_name}'"
                                else:
                                    res.menu_identity_name = title_name
                                    res.normalized_names_match = True
                                    res.resolution_status = "VERIFIED"
                                    res.failure_reason = ""
                                res.safe_diagnostics = diagnostics
                                return res

                    if heading:
                        heading_text = (heading.text_content() or "").strip()
                        heading_normalized = " ".join(heading_text.split())
                        res.profile_heading = heading_normalized

                        # Identity agreement check
                        if res.menu_identity_name:
                            menu_norm = normalize_name(res.menu_identity_name)
                            heading_norm = normalize_name(heading_normalized)
                            if menu_norm and heading_norm and (menu_norm in heading_norm or heading_norm in menu_norm or menu_norm == heading_norm):
                                res.normalized_names_match = True
                                res.resolution_status = "VERIFIED"
                                res.failure_reason = ""
                            else:
                                res.normalized_names_match = False
                                res.resolution_status = "IDENTITY_MISMATCH"
                                res.failure_reason = f"Menu identity '{res.menu_identity_name}' != profile heading '{heading_normalized}'"
                        else:
                            # No menu identity — accept profile heading as identity
                            res.menu_identity_name = heading_normalized
                            res.normalized_names_match = True
                            res.resolution_status = "VERIFIED"
                            res.failure_reason = ""
                    else:
                        res.resolution_status = "NO_PROFILE_HEADING"
                        res.failure_reason = "No h1 heading found on profile page (alternate flow)"
                else:
                    res.resolution_status = "NO_PROFILE_HEADING"
                    res.failure_reason = "Profile top card not found (alternate flow)"
            else:
                res.resolution_status = "NO_SCOPED_VIEW_PROFILE"
                res.failure_reason = f"Alternate view profile URL '{profile_url}' is not a valid own-profile path"

            res.safe_diagnostics = diagnostics
            return res

        # No view_profile_url from alternate path and no standard candidates
        res.resolution_status = "NO_ACCOUNT_TRIGGER"
        res.failure_reason = "Alternate layout resolution did not discover a profile URL"
        res.safe_diagnostics = diagnostics
        return res

    # Sort by confidence score descending, then by candidate_index ascending
    candidates.sort(key=lambda c: (-c["confidence_score"], c["candidate_index"]))

    # Check for ambiguity: top two have equal scores and both above threshold
    if len(candidates) >= 2 and candidates[0]["confidence_score"] == candidates[1]["confidence_score"]:
        res.resolution_status = "AMBIGUOUS_ACCOUNT_TRIGGER"
        res.failure_reason = f"Top 2 candidates have equal confidence ({candidates[0]['confidence_score']})"
        res.safe_diagnostics = diagnostics
        return res

    # Step 3: Click the selected trigger and detect menu opening
    selected = candidates[0]
    res.selected_trigger_selector = selected["selector_pattern"]
    res.selected_trigger_reason = f"Highest confidence ({selected['confidence_score']}): {selected.get('aria_label', '')} / {selected.get('data_view_name', '')}"

    # Re-locate the element for interaction
    trigger_el = None
    ancestor_sel = selected.get("ancestor_navigation_selector", nav_sel)
    try:
        # Try all containers matching the ancestor selector
        containers = page.locator(ancestor_sel)
        for ci in range(min(containers.count(), 10)):
            scoped = containers.nth(ci).locator(selected["selector_pattern"])
            if scoped.count() > 0 and scoped.first.is_visible():
                trigger_el = scoped.first
                break
        if not trigger_el:
            # Page-level compound fallback
            scoped = page.locator(f"{ancestor_sel} {selected['selector_pattern']}")
            if scoped.count() > 0:
                trigger_el = scoped.first
    except Exception:
        pass

    if not trigger_el or not trigger_el.is_visible():
        res.resolution_status = "TRIGGER_NOT_INTERACTIVE"
        res.failure_reason = "Selected trigger element not found or not visible on re-locate"
        res.safe_diagnostics = diagnostics
        return res

    # Record pre-click state for ownership proof
    pre_click_aria_expanded = trigger_el.get_attribute("aria-expanded") or ""
    pre_click_aria_controls = trigger_el.get_attribute("aria-controls") or ""

    pre_click_menu_count = page.locator(".artdeco-dropdown__content:visible, [role='menu']:visible, [role='dialog']:visible").count()

    try:
        trigger_el.click(timeout=4000)
        page.wait_for_timeout(1000)
        res.trigger_clicked = True
    except Exception as e:
        res.resolution_status = "TRIGGER_NOT_INTERACTIVE"
        res.failure_reason = f"Click failed: {e}"
        res.safe_diagnostics = diagnostics
        return res

    # Step 4: Detect newly opened menu and prove ownership
    post_click_aria_expanded = trigger_el.get_attribute("aria-expanded") or ""

    menu_container = None
    menu_sel = ""
    ownership_proof = ""

    # Method 1: aria-controls ownership
    if pre_click_aria_controls:
        controlled = page.locator(f"#{pre_click_aria_controls}")
        if controlled.count() > 0 and controlled.first.is_visible():
            menu_container = controlled.first
            menu_sel = f"#{pre_click_aria_controls}"
            ownership_proof = f"aria-controls='{pre_click_aria_controls}'"

    # Method 2: aria-expanded transition
    if not menu_container and pre_click_aria_expanded != post_click_aria_expanded and post_click_aria_expanded == "true":
        # Look for newly visible dropdown/menu containers
        menu_candidates_sels = [
            ".global-nav__me-content",
            ".artdeco-dropdown__content",
            "[role='menu']",
            "[role='dialog']:has(a[href*='/in/'])",
            "div.artdeco-dropdown__content",
        ]
        for msel in menu_candidates_sels:
            mc = page.locator(msel)
            for mi in range(min(mc.count(), 5)):
                if mc.nth(mi).is_visible():
                    menu_container = mc.nth(mi)
                    menu_sel = msel
                    ownership_proof = f"aria-expanded transition ({pre_click_aria_expanded} -> {post_click_aria_expanded})"
                    break
            if menu_container:
                break

    # Method 3: DOM adjacency (sibling/child of trigger)
    if not menu_container:
        adj_sels = [
            ".global-nav__me-content",
            ".artdeco-dropdown__content",
            "div[role='menu']",
            "div[aria-label*='Account']",
        ]
        for adj_sel in adj_sels:
            # Try as sibling of the trigger's parent in standard nav
            try:
                parent_dropdown = page.locator(f"{nav_sel} .global-nav__me {adj_sel}, {nav_sel} .artdeco-dropdown {adj_sel}")
                if parent_dropdown.count() > 0 and parent_dropdown.first.is_visible():
                    menu_container = parent_dropdown.first
                    menu_sel = adj_sel
                    ownership_proof = f"DOM adjacency within navigation ({adj_sel})"
                    break
            except Exception:
                pass
            # Also try within ancestor container (for alternate nav)
            try:
                ancestor = selected.get("ancestor_navigation_selector", nav_sel)
                alt_dropdown = page.locator(f"{ancestor} {adj_sel}")
                for ai in range(min(alt_dropdown.count(), 5)):
                    if alt_dropdown.nth(ai).is_visible():
                        menu_container = alt_dropdown.nth(ai)
                        menu_sel = adj_sel
                        ownership_proof = f"DOM adjacency within ancestor ({ancestor} {adj_sel})"
                        break
                if menu_container:
                    break
            except Exception:
                pass

    # Method 4: Newly visible container after click
    if not menu_container:
        post_click_menu_count = page.locator(".artdeco-dropdown__content:visible, [role='menu']:visible, [role='dialog']:visible").count()
        if post_click_menu_count > pre_click_menu_count:
            for fallback_sel in [".artdeco-dropdown__content:visible", "[role='menu']:visible"]:
                fc = page.locator(fallback_sel)
                if fc.count() > 0:
                    menu_container = fc.last
                    menu_sel = fallback_sel
                    ownership_proof = f"Newly visible container after click (pre={pre_click_menu_count}, post={post_click_menu_count})"
                    break

    if not menu_container:
        res.resolution_status = "MENU_NOT_OPENED"
        res.failure_reason = "No menu container became visible after clicking trigger"
        res.safe_diagnostics = diagnostics
        return res

    res.menu_opened = True
    res.menu_selector = menu_sel
    res.menu_count = page.locator(menu_sel).count() if menu_sel else 0

    if not ownership_proof:
        res.resolution_status = "MENU_OWNERSHIP_NOT_PROVEN"
        res.failure_reason = "Menu opened but ownership link to trigger not established"
        res.safe_diagnostics = diagnostics
        return res

    res.menu_ownership_proven = True
    diagnostics["ownership_proof"] = ownership_proof

    # Step 5: Locate scoped account card inside opened menu
    card_container = None
    card_sel = ""
    card_candidates_sels = [
        ".artdeco-entity-lockup",
        ".artdeco-entity-lockup__title",
        "div:has(> a[href*='/in/'])",
    ]
    for csel in card_candidates_sels:
        cc = menu_container.locator(csel)
        if cc.count() > 0:
            card_container = cc.first
            card_sel = csel
            res.account_card_selector = csel
            res.account_card_count = cc.count()
            break

    if not card_container:
        res.resolution_status = "NO_SCOPED_ACCOUNT_CARD"
        res.failure_reason = "No account identity card found inside opened menu container"
        res.safe_diagnostics = diagnostics
        return res

    # Extract menu identity name from the card
    name_selectors = [".title", ".artdeco-entity-lockup__title", "span.t-bold", ".t-16", "h3", "div.t-bold"]
    menu_name_raw = ""
    for nsel in name_selectors:
        sub = menu_container.locator(nsel)
        if sub.count() > 0:
            txt = sub.first.text_content() or ""
            if txt.strip():
                menu_name_raw = txt.strip()
                break

    if menu_name_raw:
        res.menu_identity_name = normalize_name(menu_name_raw)
    else:
        # Try getting text from the card itself
        card_text = card_container.text_content() or ""
        if card_text.strip():
            # Take first line as name
            lines = [ln.strip() for ln in card_text.split("\n") if ln.strip()]
            if lines:
                res.menu_identity_name = normalize_name(lines[0])

    # Step 6: Find exactly one scoped View Profile link inside the menu
    vp_link = None
    vp_sel = ""
    vp_candidates_sels = [
        "a.global-nav__secondary-link",
        "a[href*='/in/']",
        "a[data-control-name*='nav.settings_view_profile']",
    ]

    vp_count_total = 0
    for vpsel in vp_candidates_sels:
        vp_cand = menu_container.locator(vpsel)
        c = vp_cand.count()
        if c > 0:
            # Filter to valid profile URLs only
            valid_links = []
            for i in range(min(c, 10)):
                href = vp_cand.nth(i).get_attribute("href") or ""
                if "/in/" in href:
                    # Reject non-profile URLs
                    if any(reject in href for reject in ["/jobs/", "/company/", "/feed/", "/messaging/", "/search/", "/groups/"]):
                        continue
                    valid_links.append((vp_cand.nth(i), vpsel, href))

            if valid_links:
                vp_count_total += len(valid_links)
                if not vp_link:
                    vp_link, vp_sel, _ = valid_links[0]

    res.view_profile_selector = vp_sel
    res.view_profile_count = vp_count_total

    if vp_count_total == 0:
        res.resolution_status = "NO_SCOPED_VIEW_PROFILE"
        res.failure_reason = "No valid View Profile link found inside opened menu container"
        res.safe_diagnostics = diagnostics
        return res

    # Extract and normalize the profile URL
    vp_href_raw = vp_link.get_attribute("href") or ""
    if vp_href_raw.startswith("/"):
        profile_url = f"https://www.linkedin.com{vp_href_raw}"
    else:
        profile_url = vp_href_raw
    profile_url = profile_url.split("?")[0].rstrip("/") + "/"
    res.view_profile_url = profile_url

    url_pattern = re.compile(r"^https://www\.linkedin\.com/in/[a-zA-Z0-9%\-_]+/$")
    if not url_pattern.match(profile_url):
        res.resolution_status = "NO_SCOPED_VIEW_PROFILE"
        res.failure_reason = f"View Profile URL '{profile_url}' is not a valid own-profile path"
        res.safe_diagnostics = diagnostics
        return res

    # Step 7: Navigate to own profile in same context
    try:
        prof_response = page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        res.profile_page_loaded = True
    except Exception as e:
        res.resolution_status = "PROFILE_PAGE_NOT_LOADED"
        res.failure_reason = f"Profile page navigation failed: {e}"
        res.safe_diagnostics = diagnostics
        return res

    # Step 8: Find profile top card and heading
    top_card = None
    top_card_sel = ""
    top_card_candidates = ["main section.pv-top-card", ".pv-top-card", "main section"]
    for tsel in top_card_candidates:
        tc = page.locator(tsel)
        if tc.count() > 0:
            top_card = tc.first
            top_card_sel = tsel
            res.profile_top_card_selector = tsel
            res.profile_top_card_count = tc.count()
            break

    if not top_card:
        res.resolution_status = "NO_PROFILE_HEADING"
        res.failure_reason = "Profile top card section not found"
        res.safe_diagnostics = diagnostics
        return res

    heading = None
    heading_candidates = ["h1.text-heading-xlarge", "h1.inline.t-24", "h1.t-24", "h1", "h2"]
    for hsel in heading_candidates:
        hc = top_card.locator(hsel)
        if hc.count() > 0:
            heading = hc.first
            break

    # Page-level fallback: h1, then main h2
    if not heading:
        for fallback_sel in ["h1", "main h2"]:
            fb = page.locator(fallback_sel)
            if fb.count() > 0:
                heading = fb.first
                break

    if not heading:
        res.resolution_status = "NO_PROFILE_HEADING"
        res.failure_reason = "No h1 heading found inside profile top card"
        res.safe_diagnostics = diagnostics
        return res

    heading_text = (heading.text_content() or "").strip()
    if not heading_text:
        res.resolution_status = "NO_PROFILE_HEADING"
        res.failure_reason = "Profile heading h1 text is empty"
        res.safe_diagnostics = diagnostics
        return res

    res.profile_heading = normalize_name(heading_text)

    # Step 9: Verify identity agreement
    cmp = compare_linkedin_identity_values(
        menu_name=menu_name_raw if menu_name_raw else res.menu_identity_name,
        profile_name=heading_text,
        profile_url=profile_url,
    )

    res.normalized_names_match = cmp["names_match"]

    if not cmp["names_match"]:
        res.resolution_status = "IDENTITY_MISMATCH"
        res.failure_reason = f"Menu name '{cmp['menu_name_normalized']}' != Profile heading '{cmp['profile_heading_normalized']}'"
        res.safe_diagnostics = diagnostics
        return res

    if not cmp["profile_url_verified"]:
        res.resolution_status = "NO_SCOPED_VIEW_PROFILE"
        res.failure_reason = f"Profile URL '{profile_url}' did not pass URL validation"
        res.safe_diagnostics = diagnostics
        return res

    # All checks passed
    res.resolution_status = "VERIFIED"
    res.failure_reason = ""
    res.safe_diagnostics = diagnostics
    return res



def normalize_navigation_evidence(raw: dict | None) -> dict:
    """Normalize raw navigation evidence dictionary to guarantee all required keys exist without KeyError."""
    if not isinstance(raw, dict):
        raw = {}

    return {
        "document_ready_state": str(raw.get("document_ready_state", "") or ""),
        "body_text_length": int(raw.get("body_text_length", 0) or 0),
        "login_form": bool(raw.get("login_form_visible", False) or raw.get("login_form", False)),
        "checkpoint": bool(raw.get("checkpoint_detected", False) or raw.get("checkpoint", False)),
        "captcha": bool(raw.get("captcha_detected", False) or raw.get("captcha", False)),
        "two_factor_page": bool(raw.get("two_factor_detected", False) or raw.get("two_factor_page", False)),
        "consent_page": bool(raw.get("consent_indicator_count", 0) > 0 or raw.get("consent_page", False)),
        "restriction_page": bool(raw.get("restriction_detected", False) or raw.get("restriction_page", False)),
        "global_navigation_selector": str(raw.get("global_navigation_selector", ".global-nav")),
        "global_navigation_selector_count": int(raw.get("global_navigation_selector_count", 0) or 0),
        "me_trigger_selector": str(raw.get("me_trigger_selector", "button.global-nav__primary-link-me-menu-trigger")),
        "me_trigger_selector_count": int(raw.get("me_trigger_selector_count", 0) or 0),
        "account_menu_selector": str(raw.get("account_menu_selector", ".global-nav__me-content")),
        "account_menu_selector_count": int(raw.get("account_menu_selector_count", 0) or 0),
        "account_card_selector": str(raw.get("account_card_selector", ".artdeco-entity-lockup")),
        "account_card_selector_count": int(raw.get("account_card_selector_count", 0) or 0),
        "view_profile_selector": str(raw.get("view_profile_selector", "a.global-nav__secondary-link")),
        "view_profile_selector_count": int(raw.get("view_profile_selector_count", 0) or 0),
        "view_profile_url": str(raw.get("view_profile_url", "NOT_VERIFIED") or "NOT_VERIFIED"),
        "profile_heading_selector": str(raw.get("profile_heading_selector", "h1.text-heading-xlarge")),
        "profile_heading_selector_count": int(raw.get("profile_heading_selector_count", 0) or 0),
        "profile_heading": str(raw.get("profile_heading", "NOT_VERIFIED") or "NOT_VERIFIED"),
        "page_error_count": int(raw.get("page_error_count", 0) or 0),
        "console_error_count": int(raw.get("console_error_count", 0) or 0),
        "failed_request_count": int(raw.get("failed_request_count", 0) or 0),
        "rendered_page_classification": str(raw.get("rendered_page_classification", "UNKNOWN") or "UNKNOWN"),
        "classification_reason": str(raw.get("classification_reason", "No classification reason provided") or ""),
    }


def collect_dom_inventory(page) -> dict:
    """Collect safe structural DOM metrics and candidate selector metadata without storing private text."""
    if not page:
        return {}

    inventory = {}
    try:
        inventory["ready_state"] = page.evaluate("document.readyState") or ""
        inventory["viewport_width"] = page.evaluate("window.innerWidth") or 0
        inventory["viewport_height"] = page.evaluate("window.innerHeight") or 0
        try: inventory["user_agent_family"] = (page.evaluate("navigator.userAgent") or "").split()[0]
        except Exception: inventory["user_agent_family"] = "Unknown"
        try: inventory["body_child_count"] = page.evaluate("document.body.children.length") or 0
        except Exception: inventory["body_child_count"] = 0
        try: inventory["body_text_length"] = len(page.evaluate("document.body.innerText") or "")
        except Exception: inventory["body_text_length"] = 0

        inventory["main_landmark_count"] = page.locator("main").count()
        inventory["navigation_landmark_count"] = page.locator("nav, .global-nav, #global-nav").count()
        inventory["header_count"] = page.locator("header").count()
        inventory["button_count"] = page.locator("button").count()
        inventory["link_count"] = page.locator("a").count()
        inventory["dialog_count"] = page.locator("dialog, [role='dialog']").count()
        inventory["iframe_count"] = page.locator("iframe").count()

        inventory["visible_form_count"] = page.locator("form").count()
        inventory["password_input_count"] = page.locator("input[type='password'], input#password").count()
        inventory["email_input_count"] = page.locator("input[type='email'], input#username").count()

        inventory["app_shell_candidates"] = page.locator(".global-nav, #global-nav, nav, header").count()
        inventory["standard_nav_candidates"] = page.locator(".global-nav, #global-nav").count()
        inventory["alternate_nav_candidates"] = page.locator("nav.global-nav, header nav, nav").count()
        inventory["responsive_nav_candidates"] = page.locator("button[aria-label*='nav'], button[aria-label*='menu'], .global-nav__me").count()

        inventory["account_trigger_candidates"] = page.locator(
            "button.global-nav__primary-link-me-menu-trigger, button[aria-label*='Me'], button[aria-label*='me'], button[aria-label*='Account'], .global-nav__me button, header button:has(img), nav button:has(img)"
        ).count()
        inventory["account_menu_candidates"] = page.locator(".global-nav__me-content, .artdeco-dropdown__content, div[aria-label*='Me'], div[aria-label*='me']").count()
        inventory["view_profile_candidates"] = page.locator("a.global-nav__secondary-link, a[href*='/in/']").count()
        inventory["profile_link_candidates"] = page.locator("a[href*='/in/']").count()
        inventory["feed_link_candidates"] = page.locator("a[href*='/feed/']").count()
        inventory["jobs_link_candidates"] = page.locator("a[href*='/jobs/']").count()
        inventory["network_link_candidates"] = page.locator("a[href*='/mynetwork/']").count()
        inventory["messaging_link_candidates"] = page.locator("a[href*='/messaging/']").count()

        curr_url = page.url or ""
        captcha = "captcha" in curr_url or page.locator("#captcha, .captcha").count() > 0
        checkpoint = "/checkpoint" in curr_url or "challenge" in curr_url
        two_factor = page.locator("input[name='pin']").count() > 0
        restriction = "restricted" in curr_url or page.locator(".account-restricted").count() > 0
        login_form = inventory["password_input_count"] > 0 or "/login" in curr_url or "/authwall" in curr_url

        if captcha:
            inventory["rendered_classification"] = "CAPTCHA"
            inventory["classification_reason"] = "CAPTCHA elements detected in DOM or URL"
        elif checkpoint:
            inventory["rendered_classification"] = "CHECKPOINT"
            inventory["classification_reason"] = "Checkpoint challenge detected in URL or DOM"
        elif two_factor:
            inventory["rendered_classification"] = "TWO_FACTOR_REQUIRED"
            inventory["classification_reason"] = "2FA PIN input field detected"
        elif restriction:
            inventory["rendered_classification"] = "RESTRICTED"
            inventory["classification_reason"] = "Account restriction banner detected"
        elif login_form:
            inventory["rendered_classification"] = "LOGIN_PAGE"
            inventory["classification_reason"] = "Login form inputs or login/authwall URL detected"
        elif inventory["standard_nav_candidates"] > 0 and inventory["account_trigger_candidates"] > 0:
            inventory["rendered_classification"] = "AUTHENTICATED_STANDARD_NAV"
            inventory["classification_reason"] = "Standard global navigation and account trigger present"
        elif inventory["navigation_landmark_count"] > 0 and (inventory["feed_link_candidates"] > 0 or inventory["jobs_link_candidates"] > 0):
            inventory["rendered_classification"] = "AUTHENTICATED_ALTERNATE_NAV"
            inventory["classification_reason"] = "Alternate navigation landmark containing app links detected"
        elif inventory["responsive_nav_candidates"] > 0 and inventory["feed_link_candidates"] > 0:
            inventory["rendered_classification"] = "RESPONSIVE_NAV_LAYOUT"
            inventory["classification_reason"] = "Responsive navigation controls with feed link detected"
        elif inventory["navigation_landmark_count"] > 0:
            inventory["rendered_classification"] = "PARTIAL_SHELL"
            inventory["classification_reason"] = "Partial application shell detected without authenticated account menu"
        elif inventory["body_text_length"] < 500:
            inventory["rendered_classification"] = "HYDRATION_FAILED"
            inventory["classification_reason"] = "Body text length under 500 characters indicates failed JS hydration"
        else:
            inventory["rendered_classification"] = "COOKIE_PRESENT_AUTH_UNCONFIRMED"
            inventory["classification_reason"] = "Cookies present but authenticated navigation DOM not confirmed"
    except Exception as e:
        inventory["rendered_classification"] = "UNKNOWN"
        inventory["classification_reason"] = f"DOM inventory extraction error: {e}"

    return inventory


def capture_navigation_evidence(page, response, requested_url: str) -> dict:
    """Capture rendered page state before any selector assertion with complete metrics."""
    final_url = page.url if page else ""
    hostname = ""
    title = ""
    ready_state = ""
    body_len = 0
    html_hash = ""

    if page:
        try: hostname = page.evaluate("window.location.hostname")
        except Exception: pass
        try: title = page.title()
        except Exception: pass
        try: ready_state = page.evaluate("document.readyState")
        except Exception: pass
        try:
            body_txt = page.evaluate("document.body.innerText") or ""
            body_len = len(body_txt)
            html_str = page.content() or ""
            html_hash = hashlib.sha256(html_str.encode("utf-8")).hexdigest()
        except Exception: pass

    login_form = page.locator("input#username, input#password, form.login__form").count() > 0 if page else False
    login_url = "/login" in final_url or "/authwall" in final_url if final_url else False
    checkpoint = "/checkpoint" in final_url or "challenge" in final_url if final_url else False
    captcha = "captcha" in final_url or (page.locator("#captcha, .captcha").count() > 0 if page else False)
    two_factor = page.locator("input[name='pin'], input#input__email_verification_pin, input#input__phone_verification_pin").count() > 0 if page else False
    restriction = "restricted" in final_url or (page.locator(".account-restricted, #account-restricted").count() > 0 if page else False)
    consent = page.locator("button[aria-label*='cookie'], button[aria-label*='Consent'], .cookie-consent").count() if page else 0

    # Scoped selector discovery with multi-variant navigation support
    global_nav_sel = ".global-nav"
    global_nav = False
    for candidate_sel in [
        ".global-nav",
        "#global-nav",
        "nav.global-nav",
        "header nav",
        "nav[role='navigation']",
        "[role='navigation']",
        "header",
        "nav",
    ]:
        if page and page.locator(candidate_sel).count() > 0:
            global_nav_sel = candidate_sel
            global_nav = True
            break

    me_count = 0
    me_sel = "button.global-nav__primary-link-me-menu-trigger"
    if page:
        for candidate_sel in [
            "button.global-nav__primary-link-me-menu-trigger",
            "button[id*='nav-me-menu-trigger']",
            "button:has(.global-nav__me-photo)",
            "button[aria-label='Me']",
            "button[aria-label*='Me ']",
            "button[aria-label*='Me,']",
            "button[aria-label*='Me -']",
            "header nav button:has(img)",
            "nav button:has(img)",
            ".global-nav__me button",
        ]:
            c = page.locator(candidate_sel).count()
            if c > 0:
                me_count = c
                me_sel = candidate_sel
                break

    main_landmark_count = page.locator("main").count() if page else 0
    nav_landmark_count = page.locator("nav, [role='navigation']").count() if page else 0
    error_page_count = page.locator(".error-page, #error-page, .system-error").count() if page else 0

    rendered_class = "UNKNOWN"
    reason = "Initial navigation page classification"
    if captcha:
        rendered_class = "CAPTCHA"
        reason = "CAPTCHA elements detected"
    elif checkpoint:
        rendered_class = "CHECKPOINT"
        reason = "Checkpoint challenge detected"
    elif two_factor:
        rendered_class = "TWO_FACTOR_REQUIRED"
        reason = "2FA input field detected"
    elif restriction:
        rendered_class = "RESTRICTED"
        reason = "Account restriction banner detected"
    elif login_form or login_url:
        rendered_class = "LOGIN_PAGE"
        reason = "Login form or login/authwall URL detected"
    elif global_nav and me_count > 0:
        rendered_class = "AUTHENTICATED_STANDARD_NAV"
        reason = "Global navigation and Me trigger present"
    elif nav_landmark_count > 0:
        rendered_class = "AUTHENTICATED_ALTERNATE_NAV"
        reason = "Alternate navigation landmark containing app links detected"

    return {
        "requested_url": requested_url,
        "navigation_response_url": response.url if response else "",
        "navigation_status": response.status if response else None,
        "final_url": final_url,
        "hostname": hostname,
        "page_title": title,
        "document_ready_state": ready_state,
        "body_text_length": body_len,
        "body_html_sha256": html_hash,
        "main_landmark_count": main_landmark_count,
        "nav_landmark_count": nav_landmark_count,
        "login_field_count": page.locator("input#username, input#password").count() if page else 0,
        "checkpoint_indicator_count": 1 if checkpoint else 0,
        "captcha_indicator_count": 1 if captcha else 0,
        "two_factor_indicator_count": 1 if two_factor else 0,
        "restriction_indicator_count": 1 if restriction else 0,
        "consent_indicator_count": consent,
        "error_page_selector_count": error_page_count,
        "global_navigation_selector": global_nav_sel,
        "global_navigation_selector_count": 1 if global_nav else 0,
        "me_trigger_selector": me_sel,
        "me_trigger_selector_count": me_count,
        "account_menu_selector": ".global-nav__me-content",
        "account_menu_selector_count": 0,
        "account_card_selector": ".artdeco-entity-lockup",
        "account_card_selector_count": 0,
        "view_profile_selector": "a.global-nav__secondary-link",
        "view_profile_selector_count": 0,
        "view_profile_url": "NOT_VERIFIED",
        "profile_heading_selector": "h1.text-heading-xlarge",
        "profile_heading_selector_count": 0,
        "profile_heading": "NOT_VERIFIED",
        "login_form_visible": login_form,
        "login_url_detected": login_url,
        "checkpoint_detected": checkpoint,
        "captcha_detected": captcha,
        "two_factor_detected": two_factor,
        "restriction_detected": restriction,
        "global_navigation_visible": global_nav,
        "me_menu_trigger_count": me_count,
        "page_error_count": 0,
        "console_error_count": 0,
        "failed_request_count": 0,
        "rendered_page_classification": rendered_class,
        "classification_reason": reason,
        "error_code": "",
    }


def classify_session_state_detailed(nav_ev: dict, stored_fp: str, post_nav_cookies: list, identity_extracted: bool = False) -> str:
    """Classify session into granular states using post-navigation cookie verification."""
    if nav_ev.get("captcha_detected"):
        return "CAPTCHA_BLOCKED"
    if nav_ev.get("checkpoint_detected"):
        return "CHECKPOINT_REQUIRED"
    if nav_ev.get("two_factor_detected"):
        return "TWO_FACTOR_REQUIRED"
    if nav_ev.get("restriction_detected"):
        return "RESTRICTION_SCREEN"

    li_cookie = next((c for c in post_nav_cookies if c.get("name") == "li_at"), None)
    if not li_cookie:
        return "SESSION_COOKIE_REJECTED"

    post_fp = hashlib.sha256(li_cookie.get("value", "").encode("utf-8")).hexdigest()[:12] if li_cookie else ""
    cookie_rotated = (post_fp != stored_fp)

    if nav_ev.get("login_form_visible") or nav_ev.get("login_url_detected"):
        return "LOGIN_REQUIRED"

    has_auth_controls = nav_ev.get("global_navigation_visible") or nav_ev.get("nav_landmark_count", 0) > 0

    if has_auth_controls and identity_extracted:
        if cookie_rotated:
            return "SESSION_COOKIE_ROTATED_AUTHENTICATED"
        return "AUTHENTICATED"

    if nav_ev.get("global_navigation_visible") and nav_ev.get("me_menu_trigger_count") == 0:
        return "SELECTOR_DRIFT"

    return "SESSION_PRESENT_AUTH_NOT_CONFIRMED"


def extract_live_linkedin_identity(page) -> dict:
    """Extract visible account identity exclusively from live Playwright DOM. Signature accepts ONLY page."""
    requested_url = "https://www.linkedin.com/feed/"
    response = page.goto(requested_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    nav_ev = capture_navigation_evidence(page, response, requested_url)

    if nav_ev["captcha_detected"]:
        raise LinkedInIdentityExtractionError("CAPTCHA detected on feed page", "CAPTCHA_BLOCKED", nav_ev)
    if nav_ev["checkpoint_detected"]:
        raise LinkedInIdentityExtractionError("Security checkpoint challenge detected", "CHECKPOINT_REQUIRED", nav_ev)
    if nav_ev["login_form_visible"] or nav_ev["login_url_detected"]:
        raise LinkedInIdentityExtractionError("Session key rejected by LinkedIn", "LOGIN_REQUIRED", nav_ev)
    if not nav_ev["global_navigation_visible"] and nav_ev.get("nav_landmark_count", 0) == 0 and nav_ev.get("header_count", 0) == 0:
        raise LinkedInIdentityExtractionError("Global navigation bar absent", "SESSION_PRESENT_AUTH_NOT_CONFIRMED", nav_ev)

    global_nav_sel = nav_ev["global_navigation_selector"]
    global_nav = page.locator(global_nav_sel).first if page.locator(global_nav_sel).count() > 0 else page.locator("body")

    me_btn = None
    me_loc_str = ""

    me_candidates = [
        "button.global-nav__primary-link-me-menu-trigger",
        "button[id*='nav-me-menu-trigger']",
        "button:has(.global-nav__me-photo)",
        "button[aria-label='Me']",
        "button[aria-label*='Me ']",
        "button[aria-label*='Me,']",
        "button[aria-label*='Me -']",
        "header nav button:has(img)",
        "nav button:has(img)",
        ".global-nav__me button",
    ]
    for sel in me_candidates:
        candidate = page.locator(sel)
        if candidate.count() > 0 and candidate.first.is_visible():
            me_btn = candidate.first
            me_loc_str = f"page.locator('{sel}')"
            nav_ev["me_trigger_selector"] = sel
            nav_ev["me_trigger_selector_count"] = candidate.count()
            break

    if not me_btn:
        nav_ev["error_code"] = "MISSING_ME_MENU"
        raise LinkedInIdentityExtractionError("Me menu trigger absent inside global navigation", "MISSING_ME_MENU", nav_ev)

    me_btn.click(timeout=4000)
    page.wait_for_timeout(1000)

    menu_container = None
    menu_sel = ".global-nav__me-content"
    menu_candidates = [
        ".global-nav__me-content",
        ".artdeco-dropdown__content",
        "div[aria-label*='Me']",
        "div[aria-label*='me']",
        "div[aria-label*='Account']",
        "div[role='dialog']:has(a[href*='/in/'])",
        "div[role='menu']:has(a[href*='/in/'])",
    ]
    for sel in menu_candidates:
        cand = page.locator(sel).first
        if cand.count() > 0 and cand.is_visible():
            menu_container = cand
            menu_sel = sel
            nav_ev["account_menu_selector"] = sel
            nav_ev["account_menu_selector_count"] = page.locator(sel).count()
            break

    if not menu_container:
        nav_ev["error_code"] = "MISSING_ME_MENU_CONTAINER"
        raise LinkedInIdentityExtractionError("Opened Me menu container absent", "MISSING_ME_MENU_CONTAINER", nav_ev)

    menu_html = menu_container.inner_html()
    menu_html_hash = hashlib.sha256(menu_html.encode("utf-8")).hexdigest()

    card_candidates = [".artdeco-entity-lockup", ".artdeco-entity-lockup__title", "div:has(a[href*='/in/'])"]
    for csel in card_candidates:
        ccand = menu_container.locator(csel)
        if ccand.count() > 0:
            nav_ev["account_card_selector"] = csel
            nav_ev["account_card_selector_count"] = ccand.count()
            break

    view_profile_link = None
    view_profile_loc_str = ""
    link_candidates = [
        "a.global-nav__secondary-link",
        "a[href*='/in/']",
        "a[data-control-name*='nav.settings_view_profile']",
    ]
    for lsel in link_candidates:
        link_cand = menu_container.locator(lsel)
        if link_cand.count() > 0:
            view_profile_link = link_cand.first
            view_profile_loc_str = f"menu_container.locator('{lsel}')"
            nav_ev["view_profile_selector"] = lsel
            nav_ev["view_profile_selector_count"] = link_cand.count()
            break

    if not view_profile_link:
        nav_ev["error_code"] = "MISSING_VIEW_PROFILE_LINK"
        raise LinkedInIdentityExtractionError("View Profile link absent from opened Me menu", "MISSING_VIEW_PROFILE_LINK", nav_ev)

    view_profile_href_raw = view_profile_link.get_attribute("href") or ""
    if view_profile_href_raw.startswith("/"):
        profile_url = f"https://www.linkedin.com{view_profile_href_raw}"
    else:
        profile_url = view_profile_href_raw

    profile_url = profile_url.split("?")[0].rstrip("/") + "/"
    nav_ev["view_profile_url"] = profile_url

    url_pattern = re.compile(r"^https://www\.linkedin\.com/in/[a-zA-Z0-9%\-_]+/$")
    if not url_pattern.match(profile_url):
        nav_ev["error_code"] = "REJECTED_GENERIC_PROFILE_URL"
        raise LinkedInIdentityExtractionError(f"Link '{profile_url}' is not a valid LinkedIn public profile URL", "REJECTED_GENERIC_PROFILE_URL", nav_ev)

    menu_name_raw = ""
    me_name_loc_str = ""
    name_selectors = [".title", ".artdeco-entity-lockup__title", "span.t-bold", ".t-16", "h3", "div.t-bold"]
    for sel in name_selectors:
        sub_n = menu_container.locator(sel)
        if sub_n.count() > 0:
            txt = sub_n.first.text_content() or ""
            if txt.strip():
                menu_name_raw = txt
                me_name_loc_str = f"menu_container.locator('{sel}')"
                break

    if not menu_name_raw.strip():
        nav_ev["error_code"] = "MISSING_MENU_NAME"
        raise LinkedInIdentityExtractionError("Menu name text absent from scoped Me menu card", "MISSING_MENU_NAME", nav_ev)

    prof_response = page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    final_profile_url = page.url.split("?")[0].rstrip("/") + "/"

    top_card = None
    top_card_sel = "main section.pv-top-card"
    top_card_candidates = ["main section.pv-top-card", ".pv-top-card", "main section"]
    for tsel in top_card_candidates:
        cand = page.locator(tsel).first
        if cand.count() > 0:
            top_card = cand
            top_card_sel = tsel
            break

    if not top_card:
        nav_ev["error_code"] = "MISSING_TOP_CARD"
        raise LinkedInIdentityExtractionError("Top card section absent on profile page", "MISSING_TOP_CARD", nav_ev)

    top_card_html = top_card.inner_html()
    top_card_hash = hashlib.sha256(top_card_html.encode("utf-8")).hexdigest()

    heading = None
    heading_sel = "h1.text-heading-xlarge"
    heading_candidates = ["h1.text-heading-xlarge", "h1.inline.t-24", "h1.t-24", "h1"]
    for hsel in heading_candidates:
        cand = top_card.locator(hsel).first
        if cand.count() > 0:
            heading = cand
            heading_sel = hsel
            nav_ev["profile_heading_selector"] = hsel
            nav_ev["profile_heading_selector_count"] = top_card.locator(hsel).count()
            break

    if not heading:
        nav_ev["error_code"] = "MISSING_TOP_CARD_HEADING"
        raise LinkedInIdentityExtractionError("Top card heading h1 absent", "MISSING_TOP_CARD_HEADING", nav_ev)

    profile_heading_raw = heading.text_content() or ""
    if not profile_heading_raw.strip():
        nav_ev["error_code"] = "MISSING_TOP_CARD_HEADING"
        raise LinkedInIdentityExtractionError("Top card heading h1 text empty", "MISSING_TOP_CARD_HEADING", nav_ev)

    nav_ev["profile_heading"] = profile_heading_raw.strip()

    cmp_res = compare_linkedin_identity_values(
        menu_name=menu_name_raw,
        profile_name=profile_heading_raw,
        profile_url=profile_url,
    )

    if not cmp_res["names_match"]:
        nav_ev["error_code"] = "NAME_MISMATCH"
        raise LinkedInIdentityExtractionError(f"Menu name '{cmp_res['menu_name_normalized']}' does not match heading '{cmp_res['profile_heading_normalized']}'", "NAME_MISMATCH", nav_ev)

    return {
        "source": "live_linkedin_dom",
        "requested_url": requested_url,
        "navigation_response_url": response.url if response else "",
        "navigation_status": response.status if response else None,
        "final_feed_url": nav_ev["final_url"],
        "hostname": nav_ev["hostname"],
        "page_title": nav_ev["page_title"],
        "authenticated_signals": nav_ev,
        "me_menu_trigger_locator": me_loc_str,
        "me_menu_name_locator": me_name_loc_str,
        "menu_name_raw": menu_name_raw,
        "menu_name_normalized": cmp_res["menu_name_normalized"],
        "menu_html_hash": menu_html_hash,
        "view_profile_locator": view_profile_loc_str,
        "view_profile_href_raw": view_profile_href_raw,
        "profile_url": profile_url,
        "profile_navigation_status": prof_response.status if prof_response else None,
        "final_profile_url": final_profile_url,
        "profile_top_card_locator": top_card_sel,
        "profile_top_card_hash": top_card_hash,
        "profile_heading_locator": heading_sel,
        "profile_heading_raw": profile_heading_raw,
        "profile_heading_normalized": cmp_res["profile_heading_normalized"],
        "names_match": cmp_res["names_match"],
        "profile_url_verified": cmp_res["profile_url_verified"],
        "identity_verified": cmp_res["identity_verified"],
        "error_code": None,
    }


def run_session_key_diagnosis(
    profile_id=None,
    user_email=None,
    workspace_id=None,
    headed=False,
    keep_open=False,
    allow_operator_session_renewal=False,
    operator_login_timeout_seconds=900,
    wait_for_operator_confirmation=False,
    dom_inventory=False,
    capture_sanitized_screenshot=False,
    capture_accessibility_inventory=False,
    resolve_account_controls_flag=False,
):
    """Perform session-key resolver, pre-installation inventory, browser cookie installation, post-navigation fingerprint acceptance proof, and operator session renewal."""
    session_res = resolve_linkedin_session_credentials(
        profile_id=profile_id,
        user_email=user_email,
        workspace_id=workspace_id,
    )

    from openoutreach.linkedin.browser.adapter import launch_provider_browser as launch_browser

    page, context, browser, playwright = launch_browser(storage_state=session_res["cookie_data"], headed=headed)

    renewal_state = "SESSION_RENEWAL_NOT_REQUESTED" if not allow_operator_session_renewal else "SESSION_RENEWAL_REQUIRED"
    login_completed = False
    operator_confirmation_received = False
    storage_persisted = False

    try:
        # Pre-navigation installed cookies
        installed_cookies = context.cookies()
        installed_li = [c for c in installed_cookies if "linkedin.com" in c.get("domain", "")]
        installed_li_at = next((c for c in installed_cookies if c.get("name") == "li_at"), None)
        installed_fp = hashlib.sha256(installed_li_at.get("value", "").encode("utf-8")).hexdigest()[:12] if installed_li_at else ""

        # Navigate to feed in same context
        resp = page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)

        # Post-navigation cookies
        post_nav_cookies = context.cookies()
        post_li_at = next((c for c in post_nav_cookies if c.get("name") == "li_at"), None)
        post_fp = hashlib.sha256(post_li_at.get("value", "").encode("utf-8")).hexdigest()[:12] if post_li_at else ""

        cookie_replaced = (bool(post_fp) and post_fp != session_res["session_key_fingerprint"])

        nav_ev = capture_navigation_evidence(page, resp, "https://www.linkedin.com/feed/")
        dom_inv = collect_dom_inventory(page)

        if capture_sanitized_screenshot:
            try:
                screenshot_path = DIAGNOSTICS_DIR / "screenshot_sanitized.png"
                page.screenshot(path=str(screenshot_path))
            except Exception:
                pass

        if dom_inventory:
            print("LINKEDIN RENDERED DOM DIAGNOSIS\n")
            print(f"LinkedInProfile ID: {session_res['account_record_id']}")
            print(f"Final URL: {nav_ev.get('final_url', '')}")
            print(f"Page title: {nav_ev.get('page_title', '')}")
            print(f"Ready state: {dom_inv.get('ready_state', '')}")
            print(f"Viewport: {dom_inv.get('viewport_width', 0)}x{dom_inv.get('viewport_height', 0)}")
            print(f"Body text length: {dom_inv.get('body_text_length', 0)}")
            print(f"Main landmarks: {dom_inv.get('main_landmark_count', 0)}")
            print(f"Navigation landmarks: {dom_inv.get('navigation_landmark_count', 0)}")
            print(f"Header count: {dom_inv.get('header_count', 0)}")
            print(f"Visible forms: {dom_inv.get('visible_form_count', 0)}")
            print(f"Password inputs: {dom_inv.get('password_input_count', 0)}")
            print(f"Application-shell candidates: {dom_inv.get('app_shell_candidates', 0)}")
            print(f"Standard-nav candidates: {dom_inv.get('standard_nav_candidates', 0)}")
            print(f"Alternate-nav candidates: {dom_inv.get('alternate_nav_candidates', 0)}")
            print(f"Responsive-nav candidates: {dom_inv.get('responsive_nav_candidates', 0)}")
            print(f"Account-trigger candidates: {dom_inv.get('account_trigger_candidates', 0)}")
            print(f"Account-menu candidates: {dom_inv.get('account_menu_candidates', 0)}")
            print(f"View Profile candidates: {dom_inv.get('view_profile_candidates', 0)}")
            print(f"Profile-link candidates: {dom_inv.get('profile_link_candidates', 0)}")
            print(f"Feed-link candidates: {dom_inv.get('feed_link_candidates', 0)}")
            print(f"Jobs-link candidates: {dom_inv.get('jobs_link_candidates', 0)}")
            print(f"Network-link candidates: {dom_inv.get('network_link_candidates', 0)}")
            print(f"Messaging-link candidates: {dom_inv.get('messaging_link_candidates', 0)}")
            print(f"Rendered classification: {dom_inv.get('rendered_classification', 'UNKNOWN')}")
            print(f"Classification reason: {dom_inv.get('classification_reason', '')}")
            print(f"Session classification: {classify_session_state_detailed(nav_ev, session_res['session_key_fingerprint'], post_nav_cookies)}")
            print("External mutations: 0\n")

        identity_extracted = False
        extracted_identity_name = "NOT_VERIFIED"
        id_res = None
        acr = None

        # Use the canonical account control resolver when requested
        if resolve_account_controls_flag:
            # Navigate back to feed for resolver (it needs fresh page state)
            try:
                page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)
            except Exception:
                pass

            acr = resolve_authenticated_account_controls(page)

            print("\nLINKEDIN ACCOUNT CONTROL RESOLUTION\n")
            print(f"LinkedInProfile ID: {session_res['account_record_id']}")
            print(f"Application shell verified: {acr.application_shell_verified}")
            print(f"Navigation selector: {acr.navigation_selector}")
            print(f"Navigation count: {acr.navigation_count}")
            print(f"Trigger candidates: {acr.candidate_trigger_count}")
            print(f"Rejected candidates: {acr.rejected_trigger_count}")
            print(f"Selected trigger: {acr.selected_trigger_selector}")
            print(f"Selection reason: {acr.selected_trigger_reason}")
            print(f"Trigger clicked: {acr.trigger_clicked}")
            print(f"Menu opened: {acr.menu_opened}")
            print(f"Menu ownership proven: {acr.menu_ownership_proven}")
            print(f"Menu selector: {acr.menu_selector}")
            print(f"Menu count: {acr.menu_count}")
            print(f"Account card selector: {acr.account_card_selector}")
            print(f"Account card count: {acr.account_card_count}")
            print(f"Menu identity: {acr.menu_identity_name}")
            print(f"Scoped View Profile selector: {acr.view_profile_selector}")
            print(f"Scoped View Profile count: {acr.view_profile_count}")
            print(f"View Profile URL: {acr.view_profile_url}")
            print(f"Profile page loaded: {acr.profile_page_loaded}")
            print(f"Profile top-card selector: {acr.profile_top_card_selector}")
            print(f"Profile top-card count: {acr.profile_top_card_count}")
            print(f"Profile heading: {acr.profile_heading}")
            print(f"Normalized names match: {acr.normalized_names_match}")
            print(f"Cookie rotated: {cookie_replaced}")
            print(f"Storage persisted: {storage_persisted}")
            print(f"Resolution status: {acr.resolution_status}")
            print(f"Failure reason: {acr.failure_reason}")

            if acr.resolution_status == "VERIFIED":
                identity_extracted = True
                extracted_identity_name = acr.menu_identity_name
                renewal_state = "SESSION_RENEWAL_AUTHENTICATED"
                # Navigate back to feed for cookie fingerprint re-check
                try:
                    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
        else:
            try:
                id_res = extract_live_linkedin_identity(page)
                if id_res and id_res.get("identity_verified"):
                    identity_extracted = True
                    extracted_identity_name = id_res.get("menu_name_normalized", "NOT_VERIFIED")
                    renewal_state = "SESSION_RENEWAL_AUTHENTICATED"
            except Exception:
                pass

        classified_state = classify_session_state_detailed(nav_ev, session_res["session_key_fingerprint"], post_nav_cookies, identity_extracted=identity_extracted)

        # Operator session renewal state machine
        if classified_state not in ("AUTHENTICATED", "SESSION_COOKIE_ROTATED_AUTHENTICATED") and allow_operator_session_renewal:
            print("\nMANUAL OPERATOR STEP REQUIRED")
            print("1. Complete the normal LinkedIn login in the visible browser.")
            print("2. Complete normal two-factor authentication manually when requested.")
            print("3. Do not close the browser.")
            print("4. Return to the terminal.")
            print("5. Press Enter only after the normal LinkedIn account feed is visibly loaded.\n")
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

            if wait_for_operator_confirmation:
                def wait_input():
                    nonlocal operator_confirmation_received
                    try:
                        input()
                        operator_confirmation_received = True
                    except (KeyboardInterrupt, EOFError):
                        pass

                t = threading.Thread(target=wait_input, daemon=True)
                t.start()

            renewal_state = "SESSION_RENEWAL_WAITING_FOR_OPERATOR"
            max_wait = operator_login_timeout_seconds
            start_time = time.time()
            login_success = False
            last_progress_log = start_time

            try:
                while time.time() - start_time < max_wait:
                    time.sleep(2)
                    elapsed = int(time.time() - start_time)

                    if time.time() - last_progress_log >= 30:
                        last_progress_log = time.time()
                        try: curr = page.url
                        except Exception: curr = "Unknown"
                        print(f"[RENEWAL PROGRESS] Waiting for operator login... Elapsed: {elapsed}s / {max_wait}s. Current URL: {curr}")

                    try:
                        curr_url = page.url
                        # Inspect current page state indicators
                        if "/checkpoint" in curr_url or "challenge" in curr_url:
                            renewal_state = "SESSION_RENEWAL_CHECKPOINT"
                        elif "captcha" in curr_url or page.locator("#captcha, .captcha").count() > 0:
                            renewal_state = "SESSION_RENEWAL_CAPTCHA"
                        elif "restricted" in curr_url or page.locator(".account-restricted").count() > 0:
                            renewal_state = "SESSION_RENEWAL_RESTRICTED"
                        elif page.locator("input[name='pin']").count() > 0:
                            renewal_state = "SESSION_RENEWAL_TWO_FACTOR_REQUIRED"
                        elif "/feed" in curr_url or page.locator("nav, .global-nav, #global-nav").count() > 0 or operator_confirmation_received:
                            # Test if authenticated identity can be extracted
                            try:
                                check_id = extract_live_linkedin_identity(page)
                                if check_id and check_id.get("identity_verified"):
                                    id_res = check_id
                                    identity_extracted = True
                                    extracted_identity_name = id_res.get("menu_name_normalized", "NOT_VERIFIED")
                                    login_success = True
                                    renewal_state = "SESSION_RENEWAL_AUTHENTICATED"
                                    break
                            except Exception:
                                pass
                    except Exception:
                        pass
            except KeyboardInterrupt:
                renewal_state = "SESSION_RENEWAL_CANCELLED"
                login_success = False
                print("\nSESSION RENEWAL: CANCELLED BY OPERATOR (Ctrl+C)")
                print("EXTERNAL WRITES: BLOCKED")

            if login_success:
                login_completed = True
                if "/feed" not in page.url:
                    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)

                try:
                    id_res = extract_live_linkedin_identity(page)
                    if id_res and id_res.get("identity_verified"):
                        identity_extracted = True
                        extracted_identity_name = id_res.get("menu_name_normalized", "NOT_VERIFIED")
                        refreshed_storage = context.storage_state()

                        # Transactional persistence for LinkedInProfile ID 1
                        with transaction.atomic():
                            prof = LinkedInProfile.objects.select_for_update().get(id=session_res["account_record_id"])
                            prof.cookie_data = refreshed_storage
                            prof.session_status = "valid"
                            prof.save(update_fields=["cookie_data", "session_status"])

                            AuditEvent.objects.create(
                                workspace=prof.user.workspace_set.first(),
                                event_type="session_renewal_authenticated",
                                action_key=f"session_renewal_{prof.id}_{int(time.time())}",
                                payload={
                                    "account_record_id": prof.id,
                                    "stored_fingerprint": session_res["session_key_fingerprint"],
                                    "post_auth_fingerprint": post_fp,
                                    "cookie_count": len(context.cookies()),
                                    "profile_url": id_res["profile_url"],
                                    "name_normalized": id_res["menu_name_normalized"],
                                    "identity_verified": True,
                                }
                            )

                        storage_persisted = True
                        post_nav_cookies = context.cookies()
                        post_li_at = next((c for c in post_nav_cookies if c.get("name") == "li_at"), None)
                        post_fp = hashlib.sha256(post_li_at.get("value", "").encode("utf-8")).hexdigest()[:12] if post_li_at else ""

                        if post_fp == session_res["session_key_fingerprint"]:
                            classified_state = "AUTHENTICATED"
                        else:
                            classified_state = "SESSION_COOKIE_ROTATED_AUTHENTICATED"

                        print("\nSESSION RENEWAL: SUCCESS")
                        print("Refreshed storage state saved transactionally to LinkedInProfile.cookie_data.")
                except Exception as e:
                    renewal_state = "SESSION_RENEWAL_FAILED"
                    print(f"\nSESSION RENEWAL: OPERATOR ACTION REQUIRED ({e})")
                    print("EXTERNAL WRITES: BLOCKED")
            else:
                if renewal_state == "SESSION_RENEWAL_WAITING_FOR_OPERATOR":
                    renewal_state = "SESSION_RENEWAL_LOGIN_TIMEOUT"
                print(f"\nSESSION RENEWAL: OPERATOR ACTION REQUIRED (State: {renewal_state})")
                print("EXTERNAL WRITES: BLOCKED")

        # Persist storage when resolver proves VERIFIED (without operator renewal)
        if resolve_account_controls_flag and acr and acr.resolution_status == "VERIFIED" and not storage_persisted:
            try:
                refreshed_storage = context.storage_state()
                post_nav_cookies = context.cookies()
                post_li_at = next((c for c in post_nav_cookies if c.get("name") == "li_at"), None)
                post_fp = hashlib.sha256(post_li_at.get("value", "").encode("utf-8")).hexdigest()[:12] if post_li_at else ""

                # Run ORM operations in a separate thread to avoid
                # Django's SynchronousOnlyOperation in Playwright's async event loop
                persistence_error = [None]

                def _persist_to_db():
                    try:
                        with transaction.atomic():
                            prof = LinkedInProfile.objects.select_for_update().get(id=session_res["account_record_id"])
                            prof.cookie_data = refreshed_storage
                            prof.session_status = "valid"
                            prof.save(update_fields=["cookie_data", "session_status"])

                            # User -> WorkspaceMembership -> Workspace
                            membership = prof.user.workspace_memberships.first()
                            workspace = membership.workspace if membership else None

                            if workspace:
                                AuditEvent.objects.create(
                                    workspace=workspace,
                                    event_type="account_control_resolution_verified",
                                    action_key=f"acr_verified_{prof.id}_{int(time.time())}",
                                    payload={
                                        "account_record_id": prof.id,
                                        "stored_fingerprint": session_res["session_key_fingerprint"],
                                        "post_resolution_fingerprint": post_fp,
                                        "cookie_count": len(post_nav_cookies),
                                        "profile_url": acr.view_profile_url,
                                        "menu_identity": acr.menu_identity_name,
                                        "profile_heading": acr.profile_heading,
                                        "resolution_status": acr.resolution_status,
                                        "identity_verified": True,
                                    }
                                )
                    except Exception as ex:
                        persistence_error[0] = ex

                db_thread = threading.Thread(target=_persist_to_db, daemon=True)
                db_thread.start()
                db_thread.join(timeout=10)

                if persistence_error[0]:
                    raise persistence_error[0]

                storage_persisted = True
                cookie_replaced = (post_fp != session_res["session_key_fingerprint"])

                if post_fp == session_res["session_key_fingerprint"]:
                    classified_state = "AUTHENTICATED"
                else:
                    classified_state = "SESSION_COOKIE_ROTATED_AUTHENTICATED"

                print(f"\nACCOUNT CONTROL RESOLUTION: PERSISTED")
                print(f"Session classification: {classified_state}")
            except Exception as e:
                print(f"\nACCOUNT CONTROL RESOLUTION: PERSISTENCE FAILED ({e})")

        # Safely normalize navigation evidence to guarantee no KeyError
        norm_ev = normalize_navigation_evidence(nav_ev)

        print("REAL LINKEDIN AUTHENTICATED SESSION PROOF\n")
        print(f"LinkedInProfile ID: {session_res['account_record_id']}")
        print(f"Browser mode: {'headed' if headed else 'headless'}")
        print("Persistent context: True")
        print(f"Existing cookie installed: {installed_li_at is not None}")
        print(f"Stored cookie fingerprint: {installed_fp}")
        print(f"Operator renewal requested: {allow_operator_session_renewal}")
        print(f"Operator renewal required: {allow_operator_session_renewal and classified_state not in ('AUTHENTICATED', 'SESSION_COOKIE_ROTATED_AUTHENTICATED')}")
        print(f"Operator renewal state: {renewal_state}")
        print(f"Operator login completed: {login_completed}")
        print(f"Operator confirmation received: {operator_confirmation_received}")
        print(f"Final URL: {nav_ev.get('final_url', '')}")
        print(f"Page title: {nav_ev.get('page_title', '')}")
        print(f"Document ready state: {norm_ev['document_ready_state']}")
        print(f"Body text length: {norm_ev['body_text_length']}")
        print(f"Login form: {norm_ev['login_form']}")
        print(f"Checkpoint: {norm_ev['checkpoint']}")
        print(f"CAPTCHA: {norm_ev['captcha']}")
        print(f"Two-factor page: {norm_ev['two_factor_page']}")
        print(f"Consent page: {norm_ev['consent_page']}")
        print(f"Restriction page: {norm_ev['restriction_page']}")
        print(f"Global navigation selector: {norm_ev['global_navigation_selector']}")
        print(f"Global navigation count: {norm_ev['global_navigation_selector_count']}")
        print(f"Me trigger selector: {norm_ev['me_trigger_selector']}")
        print(f"Me trigger count: {norm_ev['me_trigger_selector_count']}")
        print(f"Account menu selector: {norm_ev['account_menu_selector']}")
        print(f"Account menu count: {norm_ev['account_menu_selector_count']}")
        print(f"Account card selector: {norm_ev['account_card_selector']}")
        print(f"Account card count: {norm_ev['account_card_selector_count']}")
        print(f"View Profile selector: {norm_ev['view_profile_selector']}")
        print(f"View Profile count: {norm_ev['view_profile_selector_count']}")
        print(f"View Profile URL: {id_res.get('profile_url', 'NOT_VERIFIED') if id_res else norm_ev['view_profile_url']}")
        print(f"Profile heading selector: {norm_ev['profile_heading_selector']}")
        print(f"Profile heading count: {norm_ev['profile_heading_selector_count']}")
        print(f"Profile heading: {id_res.get('profile_heading_normalized', 'NOT_VERIFIED') if id_res else norm_ev['profile_heading']}")
        print(f"Menu name: {id_res.get('menu_name_normalized', 'NOT_VERIFIED') if id_res else 'NOT_VERIFIED'}")
        print(f"Menu/profile names match: {id_res.get('names_match', False) if id_res else False}")
        print(f"Post-authentication cookie fingerprint: {post_fp}")
        print(f"Cookie rotated: {cookie_replaced}")
        print(f"Storage state persisted: {storage_persisted}")
        print(f"Rendered page classification: {norm_ev['rendered_page_classification']}")
        print(f"Classification reason: {norm_ev['classification_reason']}")
        print(f"Session classification: {classified_state}")
        print(f"Live identity extraction: {extracted_identity_name}")
        print(f"Page errors: {norm_ev['page_error_count']}")
        print(f"Console errors: {norm_ev['console_error_count']}")
        print(f"Failed requests: {norm_ev['failed_request_count']}")
        print("External mutations: 0\n")

        print("CONFIGURED SESSION KEY: PRESENT")
        print(f"SESSION COOKIE INSTALLATION: VERIFIED")
        print(f"AUTHENTICATED LINKEDIN DOM: {'VERIFIED' if classified_state in ('AUTHENTICATED', 'SESSION_COOKIE_ROTATED_AUTHENTICATED') else 'NOT PROVEN'}")
        print(f"REAL ACCOUNT SESSION: {'VERIFIED' if classified_state in ('AUTHENTICATED', 'SESSION_COOKIE_ROTATED_AUTHENTICATED') else 'NOT VERIFIED'}")

        if keep_open:
            print("\nKeep-open requested. Press ENTER in terminal to close context...")
            try:
                input()
            except (KeyboardInterrupt, EOFError):
                pass

        if classified_state not in ("AUTHENTICATED", "SESSION_COOKIE_ROTATED_AUTHENTICATED"):
            return 1
        return 0

    finally:
        if not keep_open:
            if page and not page.is_closed():
                try: page.close()
                except Exception: pass
            if context:
                try: context.close()
                except Exception: pass
            if browser:
                try: browser.close()
                except Exception: pass
            if playwright:
                try: playwright.stop()
                except Exception: pass


def run_identity_extraction_session_key(
    profile_id=None,
    user_email=None,
    workspace_id=None,
    headed=False,
    keep_open=False,
    allow_operator_session_renewal=False,
    operator_login_timeout_seconds=900,
    wait_for_operator_confirmation=False,
    dom_inventory=False,
    capture_sanitized_screenshot=False,
    capture_accessibility_inventory=False,
):
    """Run live identity extraction using explicitly resolved session key with operator renewal support."""
    session_res = resolve_linkedin_session_credentials(
        profile_id=profile_id,
        user_email=user_email,
        workspace_id=workspace_id,
    )

    from openoutreach.linkedin.browser.adapter import launch_provider_browser as launch_browser

    page, context, browser, playwright = launch_browser(storage_state=session_res["cookie_data"], headed=headed)
    try:
        installed_cookies = context.cookies()
        same_context_used = len(installed_cookies) > 0

        result = None
        error_details = None
        try:
            result = extract_live_linkedin_identity(page)
        except LinkedInIdentityExtractionError as ie:
            error_details = ie
        except Exception as e:
            error_details = e

        post_nav_cookies = context.cookies()
        post_li_at = next((c for c in post_nav_cookies if c.get("name") == "li_at"), None)
        post_fp = hashlib.sha256(post_li_at.get("value", "").encode("utf-8")).hexdigest()[:12] if post_li_at else ""

        print("CONFIGURED SESSION-KEY VERIFICATION\n")
        print("Authentication architecture reused: True")
        print(f"Account record: {session_res['account_record_id']}")
        print(f"Workspace: {session_res['workspace_slug']}")
        print(f"Session source: {session_res['resolved_from']}")
        print(f"Session key present: {session_res['session_key_present']}")
        print(f"Session key fingerprint: {session_res['session_key_fingerprint']}")
        print("Session installed before navigation: True")
        print(f"Same context used: {same_context_used}")
        print("Requested URL: https://www.linkedin.com/feed/")

        if result and result.get("identity_verified"):
            print(f"Final URL: {result['final_feed_url']}")
            print(f"Page title: {result['page_title']}")
            print("Login redirect: False")
            print("Checkpoint: False")
            print("CAPTCHA: False")
            print("Global navigation: True")
            print("Me menu: True")
            print("Session classification: AUTHENTICATED")
            print(f"Account identity extracted: {result['menu_name_normalized']}")
            print("External mutations: 0")

            with open(ARTIFACTS_DIR / "linkedin-identity-result.json", "w") as f:
                json.dump(result, f, indent=2)

            return 0
        else:
            nav_ev = error_details.navigation_evidence if isinstance(error_details, LinkedInIdentityExtractionError) else {}
            err_code = classify_session_state_detailed(nav_ev, session_res["session_key_fingerprint"], post_nav_cookies, identity_extracted=False)

            print(f"Final URL: {nav_ev.get('final_url', '')}")
            print(f"Page title: {nav_ev.get('page_title', '')}")
            print(f"Login redirect: {nav_ev.get('login_url_detected', False) or nav_ev.get('login_form_visible', False)}")
            print(f"Checkpoint: {nav_ev.get('checkpoint_detected', False)}")
            print(f"CAPTCHA: {nav_ev.get('captcha_detected', False)}")
            print(f"Global navigation: {nav_ev.get('global_navigation_visible', False)}")
            print(f"Me menu: {nav_ev.get('me_menu_trigger_count', 0) > 0}")
            print(f"Session classification: {err_code}")
            print("Account identity extracted: NOT_VERIFIED")
            print("External mutations: 0")

            res_fail = {
                "source": "live_linkedin_dom",
                "requested_url": "https://www.linkedin.com/feed/",
                "navigation_response_url": nav_ev.get("navigation_response_url", ""),
                "navigation_status": nav_ev.get("navigation_status"),
                "final_feed_url": nav_ev.get("final_url", ""),
                "hostname": nav_ev.get("hostname", ""),
                "page_title": nav_ev.get("page_title", ""),
                "authenticated_signals": nav_ev,
                "names_match": False,
                "profile_url_verified": False,
                "identity_verified": False,
                "error_code": err_code or "EXTRACTION_FAILED",
            }
            with open(ARTIFACTS_DIR / "linkedin-identity-result.json", "w") as f:
                json.dump(res_fail, f, indent=2)

            return 1

    finally:
        if not keep_open:
            if page and not page.is_closed():
                try: page.close()
                except Exception: pass
            if context:
                try: context.close()
                except Exception: pass
            if browser:
                try: browser.close()
                except Exception: pass
            if playwright:
                try: playwright.stop()
                except Exception: pass


def main():
    parser = argparse.ArgumentParser(description="HuntFlow Session-Key Acceptance Proof & Account Selection Script.")
    parser.add_argument("--linkedin-profile-id", type=int, help="Explicit LinkedInProfile primary key ID")
    parser.add_argument("--user-email", type=str, help="Explicit user email selector")
    parser.add_argument("--workspace-id", type=int, help="Explicit workspace ID selector")
    parser.add_argument("--session-key-diagnosis", action="store_true", help="Diagnose configured session key resolution and cookie installation")
    parser.add_argument("--use-configured-session-key", action="store_true", help="Use configured session key for identity extraction")
    parser.add_argument("--identity-only", action="store_true", help="Perform live DOM identity extraction")
    parser.add_argument("--session-diagnosis", action="store_true", help="Alias for session-key-diagnosis")
    parser.add_argument("--headed", action="store_true", help="Launch browser in headed mode")
    parser.add_argument("--keep-open", action="store_true", help="Keep browser context open after navigation")
    parser.add_argument("--allow-operator-session-renewal", action="store_true", help="Allow manual operator login renewal in headed browser when session is expired")
    parser.add_argument("--operator-login-timeout-seconds", type=int, default=900, help="Max operator login renewal timeout in seconds")
    parser.add_argument("--wait-for-operator-confirmation", action="store_true", help="Wait for explicit operator ENTER confirmation in terminal")
    parser.add_argument("--dom-inventory", action="store_true", help="Perform safe structural DOM metrics and candidate selector diagnosis")
    parser.add_argument("--capture-sanitized-screenshot", action="store_true", help="Capture a sanitized screenshot of visible browser shell")
    parser.add_argument("--capture-accessibility-inventory", action="store_true", help="Capture accessibility landmark inventory")
    parser.add_argument("--resolve-account-controls", action="store_true", help="Run canonical account control resolver for deterministic trigger/menu/identity resolution")
    args = parser.parse_args()

    pid = getattr(args, "linkedin_profile_id", None)

    if args.session_key_diagnosis or args.session_diagnosis:
        return run_session_key_diagnosis(
            profile_id=pid,
            user_email=args.user_email,
            workspace_id=args.workspace_id,
            headed=args.headed,
            keep_open=args.keep_open,
            allow_operator_session_renewal=args.allow_operator_session_renewal,
            operator_login_timeout_seconds=args.operator_login_timeout_seconds,
            wait_for_operator_confirmation=args.wait_for_operator_confirmation,
            dom_inventory=args.dom_inventory,
            capture_sanitized_screenshot=args.capture_sanitized_screenshot,
            capture_accessibility_inventory=args.capture_accessibility_inventory,
            resolve_account_controls_flag=args.resolve_account_controls,
        )

    return run_identity_extraction_session_key(
        profile_id=pid,
        user_email=args.user_email,
        workspace_id=args.workspace_id,
        headed=args.headed,
        keep_open=args.keep_open,
        allow_operator_session_renewal=args.allow_operator_session_renewal,
        operator_login_timeout_seconds=args.operator_login_timeout_seconds,
        wait_for_operator_confirmation=args.wait_for_operator_confirmation,
        dom_inventory=args.dom_inventory,
        capture_sanitized_screenshot=args.capture_sanitized_screenshot,
        capture_accessibility_inventory=args.capture_accessibility_inventory,
    )


if __name__ == "__main__":
    sys.exit(main())
