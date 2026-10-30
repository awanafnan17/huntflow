# openoutreach/linkedin/browser/connect_with_note.py
"""Local HuntFlow wrapper: send a LinkedIn connection request WITH a note.

The installed ``linkedin_cli`` package only supports sending connection requests
WITHOUT a note (``send_connection_request`` → ``_click_without_note``). This
module provides ``send_connection_request_with_note`` which:

- If *note* is blank/empty → delegates to the original no-note sender
  (preserving exact original behavior).
- If *note* is provided → opens the connect modal, clicks "Add a note",
  fills the textarea, and clicks "Send".

Imported helpers from linkedin_cli (documented for upgrade safety):
- ``linkedin_cli.actions.connect.SELECTORS`` — CSS selectors for the connect
  buttons, More dropdown, and send buttons.
- ``linkedin_cli.actions.connect._connect_direct`` — clicks the direct
  "Connect" button on the profile top card.
- ``linkedin_cli.actions.connect._connect_via_more`` — clicks
  "More → Connect" when direct button is absent.
- ``linkedin_cli.actions.connect._check_weekly_invitation_limit`` — checks
  for the weekly limit popup after clicking send.
- ``linkedin_cli.browser.nav.dump_page_html`` — saves a diagnostic HTML
  snapshot when the connect modal cannot be found.

If any of these imports break on a linkedin_cli upgrade, the wrapper will
raise an ImportError at import time — caught at the call site and logged.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# ── Selectors for the "Add a note" flow ──────────────────────────────
# These target the modal that appears after clicking Connect.
NOTE_SELECTORS = {
    "add_note_button": (
        'button[aria-label="Add a note"]:visible, '
        'button:has-text("Add a note"):visible'
    ),
    "note_textarea": (
        'textarea[name="message"]:visible, '
        'textarea#custom-message:visible, '
        'textarea[placeholder*="Add a note"]:visible, '
        'div[role="dialog"] textarea:visible'
    ),
    "send_with_note": (
        'button[aria-label="Send invitation"]:visible, '
        'button[aria-label="Send now"]:visible, '
        'div[role="dialog"] button:has-text("Send"):visible'
    ),
}


class ConnectionNoteUIError(Exception):
    """Raised when the connection note UI fails to render, accept input, or submit."""
    pass


def send_connection_request_with_note(
    session,
    profile: Dict[str, Any],
    note: str = "",
):
    """Send a LinkedIn connection request, optionally with a personalised note.

    If *note* is empty, delegates to the original ``send_connection_request``
    from linkedin_cli (no behavioral change).

    Returns a ``ProfileState`` enum value or raises ConnectionNoteUIError on failure.
    """
    from linkedin_cli.actions.connect import send_connection_request
    from linkedin_cli.enums import ProfileState

    if not note or not note.strip():
        # No note — use original sender (exact original behavior)
        return send_connection_request(session=session, profile=profile)

    # ── Validate note style before browser interaction ─────────────
    from openoutreach.core.content_style import validate_linkedin_outbound_message
    outbound_res = validate_linkedin_outbound_message(
        message_text=note.strip(),
        content_type="connection_note",
        prospect_facts=profile,
    )
    if not outbound_res["allowed"]:
        logger.error("Connection note failed canonical outbound validation at browser boundary: %s", outbound_res["errors"])
        raise ConnectionNoteUIError(f"Connection note failed outbound validation: {outbound_res['errors']}")

    # ── Note path: open modal, fill note, send ──────────────────────
    from linkedin_cli.actions.connect import (
        SELECTORS,
        _check_weekly_invitation_limit,
        _connect_direct,
        _connect_via_more,
    )
    from linkedin_cli.browser.nav import dump_page_html

    public_identifier = profile.get("public_identifier")

    # Open the Connect modal using direct helper or ProfileActionResolver fallback
    try:
        if not _connect_direct(session) and not _connect_via_more(session):
            logger.debug(
                "Standard connect button not found for %s — trying ProfileActionResolver",
                public_identifier,
            )
            from openoutreach.linkedin.browser.profile_action_resolver import resolve_and_execute_connect
            target_url = f"https://www.linkedin.com/in/{public_identifier}/"
            res = resolve_and_execute_connect(session, target_url, note_text=note)
            if res.resolution_status == "CONNECT_AVAILABLE":
                return ProfileState.PENDING
            elif res.resolution_status in ("ALREADY_CONNECTED", "PENDING_INVITATION"):
                return ProfileState.QUALIFIED
            else:
                dump_page_html(session, profile)
                raise ConnectionNoteUIError(f"ProfileActionResolver failed for {public_identifier}: {res.failure_reason}")
    except Exception as exc:
        if "Top Card section not found" in str(exc):
            logger.info("Top card not found — invoking ProfileActionResolver for alternate layout")
            from openoutreach.linkedin.browser.profile_action_resolver import resolve_and_execute_connect
            target_url = f"https://www.linkedin.com/in/{public_identifier}/"
            res = resolve_and_execute_connect(session, target_url, note_text=note)
            if res.resolution_status == "CONNECT_AVAILABLE":
                return ProfileState.PENDING
            elif res.resolution_status in ("ALREADY_CONNECTED", "PENDING_INVITATION"):
                return ProfileState.QUALIFIED
            else:
                dump_page_html(session, profile)
                raise ConnectionNoteUIError(f"ProfileActionResolver failed for {public_identifier}: {res.failure_reason}")
        else:
            raise exc

    # Try to add a note
    if not _fill_note(session, note, public_identifier):
        # Couldn't fill the note — DO NOT fall back to sending without note.
        # Dump diagnostics and raise error.
        dump_page_html(session, profile)
        raise ConnectionNoteUIError(
            f"Failed to fill connection note for {public_identifier}: "
            f"Add a note button or textarea not found on dialog."
        )

    # Click "Send" (with note)
    try:
        _click_send_with_note(session)
    except Exception as exc:
        dump_page_html(session, profile)
        raise ConnectionNoteUIError(
            f"Failed to submit connection request with note for {public_identifier}: {exc}"
        )

    _check_weekly_invitation_limit(session)

    logger.debug("Connection request submitted WITH note for %s", public_identifier)
    return ProfileState.PENDING



def _fill_note(session, note: str, public_identifier: str) -> bool:
    """Click 'Add a note', fill the textarea. Returns True on success."""
    session.wait()
    page = session.page

    # Click "Add a note" button
    add_note_btn = page.locator(NOTE_SELECTORS["add_note_button"])
    if add_note_btn.count() == 0:
        logger.debug("'Add a note' button not found for %s", public_identifier)
        return False

    add_note_btn.first.click()
    session.wait()

    # Fill the textarea
    textarea = page.locator(NOTE_SELECTORS["note_textarea"])
    if textarea.count() == 0:
        logger.debug("Note textarea not found for %s", public_identifier)
        return False

    textarea.first.fill(note)
    session.wait()
    return True


def _click_send_with_note(session):
    """Click the 'Send' button after filling a note."""
    session.wait()
    send_btn = session.page.locator(NOTE_SELECTORS["send_with_note"])
    send_btn.first.click(force=True)
    session.wait()
    logger.debug("Connection request submitted (with note)")


def _click_send_without_note(session):
    """Fallback: click 'Send now' / 'Send without a note'."""
    from linkedin_cli.actions.connect import SELECTORS

    session.wait()
    send_btn = session.page.locator(SELECTORS["send_now"])
    send_btn.first.click(force=True)
    session.wait()
    logger.debug("Connection request submitted (no note — fallback)")
