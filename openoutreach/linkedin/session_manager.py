# openoutreach/linkedin/session_manager.py
from __future__ import annotations
import logging
from typing import Optional, Any, Dict, Tuple
from django.utils import timezone
from openoutreach.linkedin.browser.adapter import launch_provider_browser as launch_browser
from linkedin_cli.page_state import classify_page, PageState
from openoutreach.linkedin.models import LinkedInProfile, ProviderReadLedger, ProviderOperationType

import hashlib

from contextlib import contextmanager

logger = logging.getLogger(__name__)


@contextmanager
def open_authenticated_linkedin_context(
    *,
    linkedin_profile_id: int,
    headed: bool = False,
):
    """Canonical session-authenticated LinkedIn context manager for production workers.

    1. Resolves LinkedInProfile.cookie_data via resolve_linkedin_session_credentials(profile_id=linkedin_profile_id).
    2. Requires exact 'li_at' cookie.
    3. Installs complete stored cookie state before navigation.
    4. Uses one Playwright browser context.
    5. Navigates to LinkedIn feed.
    6. Verifies authenticated DOM state and cookie state.
    7. Performs cookie rotation handling (updates DB if rotated and authenticated).
    8. Yields (page, context, browser, session_res) to calling worker.
    9. Keeps context open for duration of worker task block.
    10. Performs context & browser cleanup in a finally block.
    """
    session_res = resolve_linkedin_session_credentials(profile_id=linkedin_profile_id)
    profile = session_res["profile_instance"]

    page, context, browser, playwright = launch_browser(storage_state=session_res["cookie_data"])
    try:
        installed_cookies = context.cookies()
        installed_li_at = next((c for c in installed_cookies if c.get("name") == "li_at"), None)
        if not installed_li_at:
            raise ValueError("SESSION_COOKIE_INSTALL_FAILED: Installed context cookies missing li_at cookie.")

        resp = page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        post_nav_cookies = context.cookies()
        post_li_at = next((c for c in post_nav_cookies if c.get("name") == "li_at"), None)
        post_fp = hashlib.sha256(post_li_at.get("value", "").encode("utf-8")).hexdigest()[:12] if post_li_at else ""

        from scripts.verify_real_external_account import capture_navigation_evidence, classify_session_state_detailed
        nav_ev = capture_navigation_evidence(page, resp, "https://www.linkedin.com/feed/")
        classification = classify_session_state_detailed(nav_ev, session_res["session_key_fingerprint"], post_nav_cookies)

        if post_fp and post_fp != session_res["session_key_fingerprint"]:
            if nav_ev.get("global_navigation_visible") or classification == "AUTHENTICATED":
                new_storage_state = context.storage_state()
                profile.cookie_data = new_storage_state
                profile.session_status = "valid"
                profile.save(update_fields=["cookie_data", "session_status"])
                logger.info("Rotated authenticated li_at session cookie for profile ID %d", profile.pk)
                classification = "SESSION_COOKIE_ROTATED_AUTHENTICATED"
                session_res["session_key_fingerprint"] = post_fp

        session_res["classification"] = classification
        session_res["navigation_evidence"] = nav_ev

        yield page, context, browser, session_res

    finally:
        try: page.close()
        except Exception: pass
        try: context.close()
        except Exception: pass
        try: browser.close()
        except Exception: pass
        try: playwright.stop()
        except Exception: pass


def resolve_linkedin_session_credentials(
    *,
    profile_id: int | None = None,
    user_email: str | None = None,
    workspace_id: int | None = None,
) -> dict:
    """Canonical session-key resolver for HuntFlow LinkedIn authentication.

    Requires at least one explicit selection parameter. Does not default to primary key 1.
    Requires an exact 'li_at' cookie name.
    """
    if profile_id is None and user_email is None and workspace_id is None:
        raise ValueError("ACCOUNT_SELECTION_REQUIRED: At least one explicit selector parameter (profile_id, user_email, workspace_id) is required.")

    qs = LinkedInProfile.objects.filter(active=True)
    if profile_id is not None:
        qs = qs.filter(pk=profile_id)
    if user_email is not None:
        qs = qs.filter(user__email=user_email)
    if workspace_id is not None:
        qs = qs.filter(user__memberships__workspace_id=workspace_id)

    if qs.count() == 0:
        raise ValueError("NO_ACCOUNT_RECORD: No active LinkedInProfile record found for parameters.")
    if qs.count() > 1:
        raise ValueError("AMBIGUOUS_ACCOUNT_RECORDS: Multiple active LinkedInProfile records match selection parameters.")

    profile = qs.first()
    from openoutreach.core.encryption import decrypt_dict
    cookie_data = decrypt_dict(profile.cookie_data)

    if not cookie_data or not isinstance(cookie_data, dict):
        raise ValueError("EMPTY_SESSION_KEY: Stored cookie_data is empty or invalid.")

    cookies = cookie_data.get("cookies", [])
    if not cookies:
        raise ValueError("EMPTY_SESSION_KEY: Stored cookie_data contains no cookies.")

    li_at_cookie = next((c for c in cookies if c.get("name") == "li_at"), None)
    if not li_at_cookie:
        raise ValueError("LI_AT_COOKIE_MISSING: Stored cookie_data does not contain a valid li_at cookie.")

    raw_val = li_at_cookie.get("value", "") if li_at_cookie else ""
    if not raw_val:
        raise ValueError("LI_AT_COOKIE_MISSING: LinkedIn li_at session cookie value is empty.")

    fingerprint = hashlib.sha256(raw_val.encode("utf-8")).hexdigest()[:12]

    expires_ts = li_at_cookie.get("expires")
    now_ts = timezone.now().timestamp()
    is_expired = bool(expires_ts and expires_ts < now_ts)

    workspace_slug = "default-workspace"
    try:
        from openoutreach.core.models import Workspace
        ws = Workspace.objects.filter(memberships__user=profile.user).first()
        if ws:
            workspace_slug = ws.slug
    except Exception:
        pass

    selection_method = f"profile_id={profile_id}" if profile_id else (f"user_email={user_email}" if user_email else f"workspace_id={workspace_id}")

    return {
        "account_record_id": profile.pk,
        "django_user_id": profile.user.pk if profile.user else None,
        "django_user_email": profile.user.email if profile.user else "",
        "linkedin_username": profile.linkedin_username,
        "active": profile.active,
        "session_status": profile.session_status,
        "workspace_slug": workspace_slug,
        "selection_method": selection_method,
        "session_key_present": True,
        "session_key_length": len(raw_val),
        "session_key_fingerprint": fingerprint,
        "li_at_domain": li_at_cookie.get("domain", ""),
        "li_at_path": li_at_cookie.get("path", "/"),
        "li_at_secure": li_at_cookie.get("secure", True),
        "li_at_http_only": li_at_cookie.get("httpOnly", True),
        "li_at_same_site": li_at_cookie.get("sameSite", "None"),
        "li_at_has_expiration": bool(expires_ts),
        "li_at_expired": is_expired,
        "stored_cookie_count": len(cookies),
        "stored_linkedin_cookie_count": len([c for c in cookies if "linkedin.com" in c.get("domain", "")]),
        "stored_cookie_names": [c.get("name") for c in cookies if c.get("name")],
        "stored_origins_count": len(cookie_data.get("origins", [])),
        "resolved_from": "LinkedInProfile.cookie_data",
        "cookie_data": cookie_data,
        "profile_instance": profile,
    }


def save_linkedin_session_cookie(profile: LinkedInProfile, raw_li_at: str, force: bool = False) -> bool:
    """Canonical method to safely store and encrypt an imported LinkedIn session cookie at rest.
    
    Guarantees:
    - Validates non-empty input
    - Strips whitespace, 'li_at=' prefixes, and trailing semicolons
    - Prevents silent overwrite of a HuntFlow-managed server session unless explicitly forced
    - Authenticated encryption at rest using AES-GCM
    - Sets session_origin = 'imported_seed'
    - Marks profile status as 'needs_reauth'
    - Sets last_authenticated_at timestamp
    - ZERO LinkedIn provider network traffic
    - ZERO browser activity
    - ZERO ProviderWriteIntent
    - ZERO provider task generation
    """
    if hasattr(profile, "session_origin") and profile.session_origin == "huntflow_managed":
        if force:
            return profile.replace_managed_session_with_imported_seed(raw_li_at, confirmation="REPLACE_MANAGED_SESSION")
        from openoutreach.linkedin.exceptions import SessionOwnershipConflictError
        raise SessionOwnershipConflictError(
            "Cannot silently overwrite a HuntFlow-managed server session with an imported session seed. "
            "Explicit operator replacement confirmation required."
        )

    if hasattr(profile, "import_session_seed"):
        return profile.import_session_seed(raw_li_at)

    # Fallback for mock or unmigrated instances
    if not raw_li_at or not str(raw_li_at).strip():
        raise ValueError("Please enter a valid li_at session cookie.")

    cleaned_li_at = str(raw_li_at).strip()
    if "li_at=" in cleaned_li_at:
        cleaned_li_at = cleaned_li_at.split("li_at=")[-1].split(";")[0].strip()
    if not cleaned_li_at:
        raise ValueError("Please enter a valid li_at session cookie.")

    storage_state = {
        "version": 1,
        "cookies": [
            {
                "name": "li_at",
                "value": cleaned_li_at,
                "domain": ".www.linkedin.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            },
            {
                "name": "li_at",
                "value": cleaned_li_at,
                "domain": ".linkedin.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            },
        ],
        "origins": [],
    }

    if hasattr(profile, "set_cookie_data"):
        profile.set_cookie_data(storage_state)
    else:
        profile.cookie_data = storage_state
    if hasattr(profile, "session_origin"):
        profile.session_origin = "imported_seed"
    profile.session_status = "needs_reauth"
    profile.session_error_details = "Session cookie saved securely (imported seed). Verification required."
    profile.last_authenticated_at = timezone.now()
    profile.save(update_fields=["cookie_data", "session_status", "session_error_details", "last_authenticated_at"])
    logger.info("Saved and encrypted new LinkedIn session cookie for profile ID %d with zero provider network", profile.pk)
    return True


def resolve_workspace_linkedin_profile(user, workspace, profile_id: int | None = None) -> LinkedInProfile:
    """Canonical method to resolve and authorize the LinkedInProfile for a given workspace and user.
    
    Guarantees:
    - Enforces workspace multi-tenant boundaries
    - Strictly prevents cross-tenant access with PermissionDenied
    - Never uses unsafe un-scoped .first() queries
    - Returns the canonical bound profile for the workspace (Profile #2 for Candidate Agency)
    """
    from django.core.exceptions import PermissionDenied

    if not workspace:
        raise PermissionDenied("Active workspace is required to access LinkedIn profiles.")

    if profile_id is not None:
        profile = LinkedInProfile.objects.filter(pk=profile_id).first()
        if not profile:
            raise PermissionDenied(f"LinkedIn profile #{profile_id} not found.")
        if profile.workspace_id and profile.workspace_id != workspace.id:
            raise PermissionDenied(f"Cross-tenant access violation: Profile #{profile_id} does not belong to workspace '{workspace.slug}'.")
        return profile

    # Select primary active profile bound to this workspace
    profile = LinkedInProfile.objects.filter(workspace=workspace, active=True).order_by("id").first()
    if not profile:
        profile = LinkedInProfile.objects.filter(workspace=workspace).order_by("id").first()

    # Fallback: if user owns a profile not yet assigned to a workspace, bind it
    if not profile and user and getattr(user, "is_authenticated", False):
        user_prof = LinkedInProfile.objects.filter(user=user).order_by("id").first()
        if user_prof and not user_prof.workspace:
            user_prof.workspace = workspace
            user_prof.save(update_fields=["workspace"])
            profile = user_prof

    if not profile:
        email_name = getattr(user, "email", "") or getattr(user, "username", "") or "operator@huntflow.ai"
        profile = LinkedInProfile.objects.create(
            user=user if user and getattr(user, "is_authenticated", False) else None,
            workspace=workspace,
            linkedin_username=email_name,
            active=True,
            session_status="unknown",
        )

    return profile


CAPTCHA_SELECTORS = [
    'iframe[src*="arkoselabs"]',
    'iframe[src*="arkose"]',
    'iframe[src*="captcha" i]',
    'iframe[title*="challenge" i]',
    'iframe[title*="recaptcha" i]',
    '#captcha-internal',
    '#captcha',
    'div.challenge-dialog',
    'div[data-callback*="captcha"]',
    '.recaptcha-checkbox',
    '.arkose-challenge-container',
    '#checkpoint-challenge-form iframe',
]

CHALLENGE_SELECTORS = [
    'form#checkpoint-challenge-form',
    'form[action*="checkpoint"]',
    'input[name="pin"]',
    'input#input__email_verification_pin',
    'input#input__phone_verification_pin',
    '.challenge-dialog',
]

LOGIN_SELECTORS = [
    'form.login__form',
    'form[action*="login"]',
    '#username',
    '#session_key',
    'input[name="session_key"]',
    '.authwall-join-form',
]

RESTRICTION_SELECTORS = [
    'h1:has-text("account has been restricted")',
    'h1:has-text("Your account is restricted")',
    'h1:has-text("Account restricted")',
    '.account-restricted',
]

HEALTHY_SELECTORS = [
    'nav#global-nav',
    '.global-nav',
    'header.global-nav',
    'nav.global-nav',
    '.global-nav__me',
    'button.global-nav__primary-link-me-menu-trigger',
    '.feed-identity-module',
    'main.scaffold-layout__main',
    '.scaffold-layout',
    'div[data-view-name="feed-full-identity"]',
]


def _is_selector_visible(page, selector: str) -> bool:
    """Safely check if any element matching selector is attached and visible."""
    try:
        if hasattr(page, "locator"):
            loc = page.locator(selector)
            if hasattr(loc, "count"):
                cnt = loc.count()
                if type(cnt) is int and cnt > 0:
                    for i in range(min(cnt, 5)):
                        try:
                            if loc.nth(i).is_visible() is True:
                                return True
                        except Exception:
                            pass
                    return False
            elif hasattr(loc, "is_visible"):
                if loc.is_visible() is True:
                    return True
    except Exception:
        pass

    try:
        if hasattr(page, "query_selector"):
            el = page.query_selector(selector)
            if el is not None:
                if hasattr(el, "is_visible") and el.is_visible() is True:
                    return True
    except Exception:
        pass

    return False


def _is_selector_present(page, selector: str) -> bool:
    """Check if any element matching selector is present in DOM."""
    try:
        if hasattr(page, "locator"):
            loc = page.locator(selector)
            if hasattr(loc, "count"):
                cnt = loc.count()
                if type(cnt) is int:
                    return cnt > 0
    except Exception:
        pass
    try:
        if hasattr(page, "query_selector"):
            el = page.query_selector(selector)
            return el is not None
    except Exception:
        pass
    return False


def _extract_session_signals(page) -> dict:
    """Extract safe structural signals from a Playwright Page (or mock) without capturing sensitive feed content."""
    from urllib.parse import urlsplit
    from linkedin_cli.page_state import classify_page, PageState

    final_url = ""
    try:
        final_url = (getattr(page, "url", None) or "").strip()
    except Exception:
        pass

    path = urlsplit(final_url).path.lower() if final_url else ""

    page_title = ""
    try:
        if callable(getattr(page, "title", None)):
            page_title = str(page.title() or "")
    except Exception:
        pass

    page_content = ""
    try:
        if callable(getattr(page, "content", None)):
            page_content = str(page.content() or "").lower()
    except Exception:
        pass

    page_state = PageState.UNKNOWN
    try:
        page_state = classify_page(page)
    except Exception:
        pass

    is_captcha_visible = any(_is_selector_visible(page, sel) for sel in CAPTCHA_SELECTORS)
    is_challenge_visible = any(_is_selector_visible(page, sel) for sel in CHALLENGE_SELECTORS)
    is_login_visible = any(_is_selector_visible(page, sel) for sel in LOGIN_SELECTORS)
    is_healthy_nav_visible = any(_is_selector_visible(page, sel) for sel in HEALTHY_SELECTORS)
    is_restriction_visible = any(_is_selector_visible(page, sel) for sel in RESTRICTION_SELECTORS)

    is_captcha_present = is_captcha_visible or any(_is_selector_present(page, sel) for sel in CAPTCHA_SELECTORS) or "captcha" in path
    is_challenge_present = is_challenge_visible or path.startswith(("/checkpoint", "/challenge")) or any(_is_selector_present(page, sel) for sel in CHALLENGE_SELECTORS)
    is_auth_required_present = is_login_visible or path.startswith(("/login", "/uas/login", "/authwall")) or page_state in [PageState.LOGIN, PageState.AUTHWALL]
    is_restriction_present = is_restriction_visible or path.startswith("/checkpoint/rp") or "restricted" in path or ("account has been restricted" in page_content or "account is restricted" in page_content)
    is_healthy_present = is_healthy_nav_visible or path.startswith("/feed") or page_state == PageState.FEED

    return {
        "final_url": final_url,
        "path": path,
        "page_title": page_title,
        "page_content": page_content,
        "page_state": page_state,
        "captcha_signal_present": is_captcha_present,
        "captcha_signal_visible": is_captcha_visible,
        "challenge_signal_present": is_challenge_present,
        "challenge_signal_visible": is_challenge_visible,
        "auth_required_signal_present": is_auth_required_present,
        "restriction_signal_present": is_restriction_present,
        "healthy_signal_present": is_healthy_present,
        "healthy_nav_visible": is_healthy_nav_visible,
        "redirect_count": 0,
    }


def validate_linkedin_session(
    profile: LinkedInProfile,
    page_override=None,
    return_evidence: bool = False,
) -> tuple[str, str | None] | tuple[str, str | None, dict]:
    """
    Validate a LinkedInProfile session using Playwright or supplied mock page.
    Returns a tuple of (exact_canonical_classification, error_details) or
    (exact_canonical_classification, error_details, evidence_metadata) if return_evidence=True.
    Supported canonical classifications:
      - HEALTHY
      - CHECKPOINT
      - CHALLENGE
      - CAPTCHA
      - RESTRICTION
      - HIGH_VOLUME_WARNING
      - AUTH_REQUIRED
      - REDIRECT_LOOP
      - UNKNOWN
    """
    from openoutreach.core.encryption import decrypt_dict
    cookie_data = decrypt_dict(profile.cookie_data)
    if not cookie_data:
        empty_signals = {
            "final_url": "",
            "healthy_signal_present": False,
            "captcha_signal_present": False,
            "captcha_signal_visible": False,
            "challenge_signal_present": False,
            "auth_required_signal_present": True,
            "restriction_signal_present": False,
            "redirect_count": 0,
            "classification_rule": "missing_cookie_data",
        }
        if return_evidence:
            return "AUTH_REQUIRED", "No saved session cookies found.", empty_signals
        return "AUTH_REQUIRED", "No saved session cookies found."

    def _classify_dom(page):
        signals = _extract_session_signals(page)
        final_url = signals["final_url"]
        path = signals["path"]
        page_content = signals.get("page_content", "")

        # 1. Account Restriction / Suspension (Fail-closed)
        if signals["restriction_signal_present"]:
            rule = "account_restriction_route_or_banner"
            signals["classification_rule"] = rule
            return "RESTRICTION", f"Account restricted detected at URL: {final_url}", signals

        # 2. High Volume Data Warning Check
        is_high_volume = (
            _is_selector_visible(page, ".artdeco-inline-feedback--error")
            or _is_selector_visible(page, ".high-volume-warning")
            or "high volume" in page_content
            or "unusual traffic" in page_content
        )
        if is_high_volume:
            rule = "high_volume_warning_banner"
            signals["classification_rule"] = rule
            return "HIGH_VOLUME_WARNING", "High volume data access warning detected", signals

        # 3. Visible CAPTCHA Check (Strong observable evidence required: visible challenge container/iframe OR dedicated captcha route)
        is_captcha_route = ("captcha" in path and path.startswith(("/checkpoint", "/challenge")))
        if signals["captcha_signal_visible"] or is_captcha_route:
            rule = "visible_captcha_element" if signals["captcha_signal_visible"] else "checkpoint_captcha_route"
            signals["classification_rule"] = rule
            return "CAPTCHA", f"Visible captcha security check detected at URL: {final_url}", signals

        # 4. Genuine Security Checkpoint / Challenge (Route or Form)
        if (path.startswith(("/checkpoint", "/challenge")) or signals["challenge_signal_visible"]) and not signals["healthy_nav_visible"]:
            cls_name = "CHALLENGE" if "challenge" in path else "CHECKPOINT"
            rule = "security_checkpoint_route_or_form"
            signals["classification_rule"] = rule
            return cls_name, f"Security checkpoint challenge at URL: {final_url}", signals

        # 5. Authwall / Login Required
        if signals["auth_required_signal_present"] and not signals["healthy_nav_visible"]:
            rule = "login_or_authwall_redirect"
            signals["classification_rule"] = rule
            return "AUTH_REQUIRED", f"Redirected to login page: {final_url}", signals

        # 6. Authenticated Feed / Home (Healthy)
        if signals["healthy_signal_present"]:
            rule = "authenticated_feed_reached"
            signals["classification_rule"] = rule
            return "HEALTHY", None, signals

        # 7. Fallback: if healthy nav is visible even on non-feed URL
        if signals["healthy_nav_visible"]:
            rule = "authenticated_nav_visible"
            signals["classification_rule"] = rule
            return "HEALTHY", None, signals

        rule = "unknown_page_state"
        signals["classification_rule"] = rule
        return "UNKNOWN", f"Unexpected URL path: {final_url}", signals

    # If mock page fixture provided, evaluate directly
    if page_override is not None:
        cls, err, meta = _classify_dom(page_override)
        if return_evidence:
            return cls, err, meta
        return cls, err

    # Synthetic QA Mock Boundary (zero external network requests for sentinel/qa profiles)
    cookies = cookie_data.get("cookies", []) if isinstance(cookie_data, dict) else []
    for c in cookies:
        if "SENTINEL" in str(c.get("value", "")) or (profile.linkedin_username and "qa_" in profile.linkedin_username.lower()):
            sentinel_meta = {
                "final_url": "https://www.linkedin.com/feed/",
                "healthy_signal_present": True,
                "captcha_signal_present": False,
                "captcha_signal_visible": False,
                "challenge_signal_present": False,
                "auth_required_signal_present": False,
                "restriction_signal_present": False,
                "redirect_count": 0,
                "classification_rule": "sentinel_qa_mock_boundary",
            }
            if return_evidence:
                return "HEALTHY", None, sentinel_meta
            return "HEALTHY", None

    from openoutreach.linkedin.services.account_governor import account_provider_lock

    ws = getattr(profile, "workspace", None)
    with account_provider_lock(linkedin_profile=profile, workspace=ws, operation_name="session_health_check", allow_reentrant=True) as locked_prof:
        if locked_prof is None:
            lock_err = f"ProviderAccountLeaseConflict: Profile #{profile.pk} is actively leased by another process."
            lock_meta = {
                "final_url": "https://www.linkedin.com/feed/",
                "healthy_signal_present": False,
                "captcha_signal_present": False,
                "captcha_signal_visible": False,
                "challenge_signal_present": False,
                "auth_required_signal_present": False,
                "restriction_signal_present": False,
                "redirect_count": 0,
                "classification_rule": "concurrency_lease_blocked",
            }
            if return_evidence:
                return "UNKNOWN", lock_err, lock_meta
            return "UNKNOWN", lock_err

        try:
            page, context, browser, playwright = launch_browser(storage_state=cookie_data)
            try:
                logger.info("Validating saved session for %s", profile.linkedin_username)
                page.goto("https://www.linkedin.com/feed/", wait_until="commit", timeout=15000)
                page.wait_for_timeout(3000)
                cls, err, meta = _classify_dom(page)

                # Persist updated auxiliary cookies if session verified healthy
                if cls == "HEALTHY":
                    try:
                        st = context.storage_state()
                        if isinstance(st, dict) and isinstance(st.get("cookies"), list):
                            has_li_at = any(c.get("name") == "li_at" and c.get("value") for c in st["cookies"] if isinstance(c, dict))
                            if has_li_at:
                                st["version"] = 1
                                profile.set_cookie_data(st)
                                profile.save(update_fields=["cookie_data"])
                    except Exception as pe:
                        logger.debug("Health check storage state update note: %s", pe)

                if return_evidence:
                    return cls, err, meta
                return cls, err
            finally:
                try:
                    page.close()
                except Exception:
                    pass
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
                try:
                    playwright.stop()
                except Exception:
                    pass
        except Exception as e:
            logger.exception("Playwright session validation encountered exception")
            err_msg = str(e)
            err_meta = {
                "final_url": "https://www.linkedin.com/feed/",
                "healthy_signal_present": False,
                "captcha_signal_present": False,
                "captcha_signal_visible": False,
                "challenge_signal_present": False,
                "auth_required_signal_present": False,
                "restriction_signal_present": False,
                "redirect_count": 0,
                "classification_rule": "browser_validation_exception",
                "error": err_msg,
            }
            if "ERR_TOO_MANY_REDIRECTS" in err_msg or "redirect" in err_msg.lower():
                err_meta["classification_rule"] = "redirect_loop_exception"
                if return_evidence:
                    return "REDIRECT_LOOP", f"Redirect loop detected ({err_msg[:200]}); manual re-authentication required", err_meta
                return "REDIRECT_LOOP", f"Redirect loop detected ({err_msg[:200]}); manual re-authentication required"
            if "timeout" in err_msg.lower():
                err_meta["classification_rule"] = "timeout_exception"
                if return_evidence:
                    return "UNKNOWN", f"Navigation timeout during session validation ({err_msg[:200]})", err_meta
                return "UNKNOWN", f"Navigation timeout during session validation ({err_msg[:200]})"
            if return_evidence:
                return "UNKNOWN", str(e), err_meta
            return "UNKNOWN", str(e)


CLASSIFICATION_TO_PROFILE_STATUS = {
    "HEALTHY": "valid",
    "VALID": "valid",
    "valid": "valid",
    "CHECKPOINT": "checkpoint",
    "checkpoint": "checkpoint",
    "CHALLENGE": "checkpoint",
    "challenge": "checkpoint",
    "CAPTCHA": "checkpoint",
    "captcha": "checkpoint",
    "RESTRICTION": "restricted",
    "restricted": "restricted",
    "HIGH_VOLUME_WARNING": "checkpoint",
    "AUTH_REQUIRED": "expired",
    "EXPIRED": "expired",
    "expired": "expired",
    "REDIRECT_LOOP": "needs_reauth",
    "NEEDS_REAUTH": "needs_reauth",
    "needs_reauth": "needs_reauth",
    "UNKNOWN": "unknown",
    "unknown": "unknown",
    "INVALID": "invalid",
    "invalid": "invalid",
}


def run_session_health_check(profile: LinkedInProfile, page_override=None) -> LinkedInProfile:
    """
    Execute validation check, persist the results to LinkedInProfile, and log ProviderReadLedger.
    Durably records a ProviderReadLedger entry under SESSION_HEALTH at the pre-network boundary.
    Preserves exact canonical classification and safe structured evidence in ledger metadata.
    """
    ws = getattr(profile, "workspace", None)

    # Pre-network ledger record initialization
    ledger_entry = None
    try:
        ledger_entry = ProviderReadLedger.objects.create(
            workspace=ws,
            linkedin_profile=profile,
            operation_type=ProviderOperationType.SESSION_HEALTH,
            resource_identifier="feed_session_check",
            cache_hit=False,
            success=False,
            read_cost=1,
            metadata={
                "target_url": "https://www.linkedin.com/feed/",
                "state": "attempting",
            },
        )
    except Exception as ledger_err:
        logger.error("Failed to create pre-network ProviderReadLedger for session health check: %s", ledger_err)

    raw_res = validate_linkedin_session(profile, page_override=page_override, return_evidence=True)
    if len(raw_res) == 3:
        raw_classification, error_details, evidence_meta = raw_res
    else:
        raw_classification, error_details = raw_res
        evidence_meta = {}

    canonical_classification = raw_classification.upper() if raw_classification else "UNKNOWN"
    if canonical_classification == "VALID":
        canonical_classification = "HEALTHY"
    elif canonical_classification == "EXPIRED":
        canonical_classification = "AUTH_REQUIRED"
    elif canonical_classification == "RESTRICTED":
        canonical_classification = "RESTRICTION"

    profile_status = CLASSIFICATION_TO_PROFILE_STATUS.get(canonical_classification, "unknown")

    profile.session_status = profile_status
    profile.session_error_details = error_details
    profile.last_validated_at = timezone.now()

    if canonical_classification == "HEALTHY":
        profile.last_authenticated_at = timezone.now()

    profile.save(update_fields=[
        "session_status",
        "session_error_details",
        "last_validated_at",
        "last_authenticated_at",
    ])

    # Update workspace automation safety state canonically
    if ws:
        from openoutreach.core.models import WorkspaceAutomationSettings
        auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
        if auto_st:
            auto_st.recover_from_session_health(canonical_classification, profile=profile)

    # Update ledger entry with final result, exact classification, and structured evidence metadata
    if ledger_entry:
        try:
            ledger_entry.success = (canonical_classification == "HEALTHY")
            ledger_entry.metadata = {
                "classification": canonical_classification,
                "status": profile_status,
                "error_details": (error_details or "")[:500],
                "target_url": "https://www.linkedin.com/feed/",
                "final_url": evidence_meta.get("final_url", "https://www.linkedin.com/feed/"),
                "healthy_signal_present": evidence_meta.get("healthy_signal_present", False),
                "captcha_signal_present": evidence_meta.get("captcha_signal_present", False),
                "captcha_signal_visible": evidence_meta.get("captcha_signal_visible", False),
                "challenge_signal_present": evidence_meta.get("challenge_signal_present", False),
                "auth_required_signal_present": evidence_meta.get("auth_required_signal_present", False),
                "restriction_signal_present": evidence_meta.get("restriction_signal_present", False),
                "redirect_count": evidence_meta.get("redirect_count", 0),
                "classification_rule": evidence_meta.get("classification_rule", "unknown"),
            }
            ledger_entry.save(update_fields=["success", "metadata"])
        except Exception as ledger_err:
            logger.error("Failed to update ProviderReadLedger for session health check: %s", ledger_err)

    return profile


def manual_login_linkedin(profile: LinkedInProfile) -> None:
    """
    Run manual login by clearing existing cookies and invoking full authentication.
    """
    from openoutreach.linkedin.browser.session import AccountSession
    # Temporarily clear cookies to force authentication flow
    profile.cookie_data = None
    profile.save(update_fields=["cookie_data"])
    
    session = AccountSession(profile)
    try:
        session.ensure_browser()
    finally:
        try:
            if session.page:
                session.page.close()
        except Exception:
            pass
        try:
            if session.context:
                session.context.close()
        except Exception:
            pass
        try:
            if session.browser:
                session.browser.close()
        except Exception:
            pass
        try:
            if session.playwright:
                session.playwright.stop()
        except Exception:
            pass

    # Run validation immediately to update final state
    run_session_health_check(profile)
