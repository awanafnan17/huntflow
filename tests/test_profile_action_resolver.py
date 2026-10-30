# tests/test_profile_action_resolver.py
import pytest
from unittest.mock import MagicMock
from openoutreach.linkedin.browser.profile_action_resolver import resolve_and_execute_connect, ProfileActionResolution

def test_profile_action_resolver_404():
    session = MagicMock()
    session.page.title.return_value = "Page not found"
    session.page.url = "https://www.linkedin.com/404/"
    
    res = resolve_and_execute_connect(session, "https://www.linkedin.com/in/nonexistent/")
    assert res.resolution_status == "PROFILE_NOT_FOUND"
    assert "404" in res.failure_reason

def test_profile_action_resolver_already_connected():
    session = MagicMock()
    session.page.title.return_value = "Jane Doe | LinkedIn"
    session.page.url = "https://www.linkedin.com/in/jane-doe/"
    
    # Mock main region with Message button
    msg_loc = MagicMock()
    msg_loc.count.return_value = 1
    
    main_loc = MagicMock()
    main_loc.count.return_value = 1
    main_loc.locator.side_effect = lambda sel: msg_loc if "Message" in sel else MagicMock(count=lambda: 0)
    
    session.page.locator.return_value = main_loc
    
    res = resolve_and_execute_connect(session, "https://www.linkedin.com/in/jane-doe/")
    assert res.already_connected is True
    assert res.resolution_status == "ALREADY_CONNECTED"

def test_profile_action_resolver_pending():
    session = MagicMock()
    session.page.title.return_value = "Jane Doe | LinkedIn"
    session.page.url = "https://www.linkedin.com/in/jane-doe/"
    
    msg_loc = MagicMock(count=lambda: 0)
    pending_loc = MagicMock(count=lambda: 1)
    
    main_loc = MagicMock()
    main_loc.count.return_value = 1
    def locator_side_effect(sel):
        if "Message" in sel: return msg_loc
        if "Pending" in sel or "Withdraw" in sel: return pending_loc
        return MagicMock(count=lambda: 0)
    main_loc.locator.side_effect = locator_side_effect
    session.page.locator.return_value = main_loc
    
    res = resolve_and_execute_connect(session, "https://www.linkedin.com/in/jane-doe/")
    assert res.pending_invitation is True
    assert res.resolution_status == "PENDING_INVITATION"
