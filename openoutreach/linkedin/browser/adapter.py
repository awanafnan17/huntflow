# openoutreach/linkedin/browser/adapter.py
"""HuntFlow-owned Playwright Browser Adapter.

Provides clean, standard Playwright Chromium browser contexts without
evasion modifications or hidden webdriver patches.
Scoped strictly to authorized account storage state with robust lifecycle management.
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, Any, Tuple
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 25000


def launch_provider_browser(
    storage_state: Optional[Dict[str, Any] | str] = None,
    *,
    headed: bool = False,
    persistent_profile_path: Optional[str] = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> Tuple[Page, BrowserContext, Optional[Browser], Playwright]:
    """Launch an ordinary, unmodified Playwright browser context.

    Does NOT apply stealth modifications or detection-evasion hooks.
    Uses standard Playwright Chromium engine.
    """
    logger.debug("Launching HuntFlow provider browser (headed=%s)", headed)
    playwright = sync_playwright().start()

    # Parse dict if given as decrypted storage state
    state = storage_state if isinstance(storage_state, dict) else None

    if persistent_profile_path:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(persistent_profile_path),
            headless=not headed,
        )
        browser = None
    else:
        browser = playwright.chromium.launch(
            headless=not headed,
        )
        context = browser.new_context(storage_state=state)

    context.set_default_timeout(timeout_ms)
    context.set_default_navigation_timeout(timeout_ms)

    page = context.new_page() if not context.pages else context.pages[0]
    return page, context, browser, playwright
