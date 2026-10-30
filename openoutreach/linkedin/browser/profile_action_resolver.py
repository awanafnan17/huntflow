# openoutreach/linkedin/browser/profile_action_resolver.py
"""
Production Profile Action Resolver for OpenOutreach.
Resolves actions (Connect, Add Note, Send) on target LinkedIn profiles using
alternate layout strategies (hashed CSS, figure-based headers, responsive menus).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

@dataclass
class ProfileActionResolution:
    profile_page_verified: bool = False
    target_profile_url: str = ""
    target_identity: str = ""
    profile_region_selector: str = ""
    connection_state: str = "UNKNOWN"
    already_connected: bool = False
    pending_invitation: bool = False
    connect_action_selector: str = ""
    connect_action_count: int = 0
    connect_action_scoped: bool = False
    more_action_selector: str = ""
    more_menu_opened: bool = False
    connect_menuitem_selector: str = ""
    note_dialog_opened: bool = False
    add_note_selector: str = ""
    note_input_selector: str = ""
    send_selector: str = ""
    confirmation_state: str = ""
    resolution_status: str = "FAILED"
    failure_reason: str = ""


def resolve_and_execute_connect(
    session,
    target_url: str,
    note_text: str = ""
) -> ProfileActionResolution:
    """
    Resolves target profile controls and executes a connection request (with note if provided).
    Scoped to the profile's main identity section to ignore sidebar recommendations.
    """
    res = ProfileActionResolution(target_profile_url=target_url)
    page = session.page

    # 1. Verify profile page & extract target identity
    try:
        page.goto(target_url)
        session.wait()
    except Exception as e:
        res.failure_reason = f"Navigation failed: {e}"
        res.resolution_status = "FAILED"
        return res

    if "/404" in page.url or "Page not found" in page.title():
        res.resolution_status = "PROFILE_NOT_FOUND"
        res.failure_reason = "Profile page returned 404"
        return res

    res.profile_page_verified = True
    page_title = page.title()
    res.target_identity = page_title.replace(" | LinkedIn", "").strip()

    # 2. Scope to main profile region (ignore sidebars)
    main_region = page.locator("main section, main div:has(h1), main div:has(h2)")
    if main_region.count() == 0:
        main_region = page.locator("main")
    res.profile_region_selector = "main section"

    # Check if already connected or pending
    msg_btn = main_region.locator("button:has-text('Message'):visible, a:has-text('Message'):visible")
    if msg_btn.count() > 0:
        res.already_connected = True
        res.connection_state = "CONNECTED"
        res.resolution_status = "ALREADY_CONNECTED"
        res.failure_reason = f"{res.target_identity} is already connected"
        return res

    pending_btn = main_region.locator("button:has-text('Pending'):visible, button:has-text('Withdraw'):visible")
    if pending_btn.count() > 0:
        res.pending_invitation = True
        res.connection_state = "PENDING"
        res.resolution_status = "PENDING_INVITATION"
        res.failure_reason = f"Pending invitation already exists for {res.target_identity}"
        return res

    # 3. Locate Connect action scoped to target identity
    from openoutreach.linkedin.services.relationship_reconciliation import is_aria_matching_target
    target_first_name = res.target_identity.split()[0] if res.target_identity else ""
    
    # Try scoped direct Connect button inside main identity card
    connect_selectors = [
        f"button[aria-label*='Invite {target_first_name}']:visible",
        f"button[aria-label*='Invite {res.target_identity}']:visible",
        f"main button:has-text('Connect'):visible",
        "button[aria-label*='to connect']:visible",
    ]

    connect_btn = None
    for sel in connect_selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            for idx in range(loc.count()):
                candidate = loc.nth(idx)
                raw_aria = candidate.get_attribute("aria-label")
                aria = raw_aria if isinstance(raw_aria, str) else ""
                is_valid, is_unrelated = is_aria_matching_target(aria, res.target_identity, "")
                if is_valid and not is_unrelated:
                    connect_btn = candidate
                    res.connect_action_selector = sel
                    res.connect_action_count = loc.count()
                    res.connect_action_scoped = True
                    break
            if connect_btn:
                break

    # If no direct connect button, try 'More' dropdown
    if not connect_btn:
        more_selectors = [
            "main button[aria-label='More actions']:visible",
            "main button[aria-label='More']:visible",
            "main button:has-text('More'):visible"
        ]
        more_btn = None
        for sel in more_selectors:
            loc = page.locator(sel)
            if loc.count() > 0:
                more_btn = loc.first
                res.more_action_selector = sel
                break
        
        if more_btn:
            more_btn.click()
            session.wait()
            res.more_menu_opened = True
            
            menu_connect_selectors = [
                "div[role='button']:has-text('Connect'):visible",
                "li:has-text('Connect'):visible",
                "span:has-text('Connect'):visible",
                "[role='menuitem']:has-text('Connect'):visible"
            ]
            for sel in menu_connect_selectors:
                loc = page.locator(sel)
                if loc.count() > 0:
                    connect_btn = loc.first
                    res.connect_menuitem_selector = sel
                    break

    if not connect_btn:
        res.resolution_status = "CONNECT_NOT_AVAILABLE"
        res.failure_reason = f"Connect button not found for {res.target_identity}"
        return res

    # 4. Click Connect
    connect_btn.click()
    session.wait()

    # 5. Handle Note Modal if note_text provided
    if note_text:
        add_note_selectors = [
            "button[aria-label='Add a note']:visible",
            "button:has-text('Add a note'):visible"
        ]
        add_note_btn = None
        for sel in add_note_selectors:
            loc = page.locator(sel)
            if loc.count() > 0:
                add_note_btn = loc.first
                res.add_note_selector = sel
                break

        if add_note_btn:
            add_note_btn.click()
            session.wait()
            res.note_dialog_opened = True

            note_input_selectors = [
                "textarea[name='message']:visible",
                "textarea#custom-message:visible",
                "div[role='dialog'] textarea:visible"
            ]
            note_input = None
            for sel in note_input_selectors:
                loc = page.locator(sel)
                if loc.count() > 0:
                    note_input = loc.first
                    res.note_input_selector = sel
                    break

            if note_input:
                note_input.fill(note_text)
                session.wait()

                send_selectors = [
                    "button[aria-label='Send invitation']:visible",
                    "button[aria-label='Send now']:visible",
                    "div[role='dialog'] button:has-text('Send'):visible"
                ]
                send_btn = None
                for sel in send_selectors:
                    loc = page.locator(sel)
                    if loc.count() > 0:
                        send_btn = loc.first
                        res.send_selector = sel
                        break

                if send_btn:
                    send_btn.click()
                    session.wait()
                    res.confirmation_state = "INVITATION_SENT"
                    res.resolution_status = "CONNECT_AVAILABLE"
                else:
                    res.failure_reason = "Send invitation button not found"
                    res.resolution_status = "FAILED"
                    return res
            else:
                res.failure_reason = "Note textarea not found"
                res.resolution_status = "FAILED"
                return res
        else:
            # Fallback if no add note dialog appeared
            res.confirmation_state = "SENT_WITHOUT_NOTE"
            res.resolution_status = "CONNECT_AVAILABLE"
    else:
        # Send without note
        send_now_btn = page.locator("button:has-text('Send now'):visible, button[aria-label*='Send without']:visible")
        if send_now_btn.count() > 0:
            send_now_btn.first.click()
            session.wait()
        res.confirmation_state = "INVITATION_SENT"
        res.resolution_status = "CONNECT_AVAILABLE"

    return res
