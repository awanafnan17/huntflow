# openoutreach/linkedin/browser/launch.py
"""Persist + orchestrate the daemon's LinkedIn browser session.

Cookie persistence (to the Django DB) and the launch/login orchestration are
OpenOutreach concerns, so they live here. The reusable *mechanics* — launching a
stealthed browser, driving the login form, clearing checkpoints — stay in the
Django-free ``linkedin_cli.browser`` library and are called from here.
"""
from __future__ import annotations

import logging

from termcolor import colored

from linkedin_cli.auth import authenticate
from linkedin_cli.browser.login import dismiss_comply_gate
from openoutreach.linkedin.browser.adapter import launch_provider_browser as launch_browser
from linkedin_cli.browser.nav import goto_page

logger = logging.getLogger(__name__)

LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"


def _save_cookies(session):
    """Persist Playwright storage state (cookies) to the DB using canonical session persistence."""
    if hasattr(session, "persist_storage_state"):
        session.persist_storage_state()
    else:
        state = session.context.storage_state()
        if hasattr(session.linkedin_profile, "set_cookie_data"):
            session.linkedin_profile.set_cookie_data(state)
        else:
            session.linkedin_profile.cookie_data = state
        session.linkedin_profile.save(update_fields=["cookie_data"])


def start_browser_session(session):
    logger.debug("Configuring browser for %s", session)

    session.linkedin_profile.refresh_from_db(fields=["cookie_data"])
    cookie_data = session.linkedin_profile.get_decrypted_cookie_data() if hasattr(session.linkedin_profile, "get_decrypted_cookie_data") else session.linkedin_profile.cookie_data
    if cookie_data and isinstance(cookie_data, str) and (cookie_data.startswith("gcm_enc::") or cookie_data.startswith("v1::")):
        from openoutreach.core.encryption import decrypt_dict
        cookie_data = decrypt_dict(cookie_data)

    storage_state = cookie_data if isinstance(cookie_data, dict) else None
    if storage_state:
        logger.info("Loading saved session for %s (storage_state with %s cookies)", session, len(storage_state.get("cookies", [])))

    session.page, session.context, session.browser, session.playwright = launch_browser(storage_state=storage_state)

    from openoutreach.linkedin.models import ProviderOperationType
    from openoutreach.linkedin.services.evidence_cache import record_provider_read

    lp = session.linkedin_profile
    ws = getattr(lp, "workspace", None) or (getattr(session, "campaign", None) and session.campaign.workspace)

    if not storage_state:
        try:
            authenticate(session, username=lp.linkedin_username, password=lp.linkedin_password)
            _save_cookies(session)
            logger.info(colored("Login successful – session saved", "green", attrs=["bold"]))
            record_provider_read(
                operation_type=ProviderOperationType.SESSION_HEALTH,
                resource_identifier="session_login",
                cache_hit=False,
                linkedin_profile=lp,
                workspace=ws,
                success=True,
                read_cost=1,
                metadata={"url": session.page.url if session.page else LINKEDIN_FEED_URL, "type": "fresh_login"},
            )
        except Exception as exc:
            record_provider_read(
                operation_type=ProviderOperationType.SESSION_HEALTH,
                resource_identifier="session_login",
                cache_hit=False,
                linkedin_profile=lp,
                workspace=ws,
                success=False,
                read_cost=1,
                metadata={"error": str(exc), "type": "fresh_login"},
            )
            raise exc
    else:
        try:
            try:
                session.page.goto(LINKEDIN_FEED_URL, wait_until="commit", timeout=25000)
                session.page.wait_for_timeout(2500)
            except Exception as e:
                logger.debug("Initial feed load note: %s", e)
            dismiss_comply_gate(session.page)
            goto_page(
                session,
                action=lambda: None,
                expected_url_pattern="/feed",
                error_message="Saved session invalid",
            )
            try:
                session.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            _save_cookies(session)
            record_provider_read(
                operation_type=ProviderOperationType.SESSION_HEALTH,
                resource_identifier="session_init:/feed",
                cache_hit=False,
                linkedin_profile=lp,
                workspace=ws,
                success=True,
                read_cost=1,
                metadata={"url": session.page.url if session.page else LINKEDIN_FEED_URL, "type": "saved_session"},
            )
        except Exception as exc:
            record_provider_read(
                operation_type=ProviderOperationType.SESSION_HEALTH,
                resource_identifier="session_init:/feed",
                cache_hit=False,
                linkedin_profile=lp,
                workspace=ws,
                success=False,
                read_cost=1,
                metadata={"error": str(exc), "stage": "session_init_feed", "url": session.page.url if session.page else LINKEDIN_FEED_URL, "type": "saved_session"},
            )
            raise exc

    logger.info(colored("Browser ready", "green", attrs=["bold"]))
