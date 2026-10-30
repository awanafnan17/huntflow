# openoutreach/contacts/service.py
"""The central contacts store (stub) — deactivated for HuntFlow rebrand."""
from __future__ import annotations

DEFAULT_API_URL = "https://hub.openoutreach.app"
ORIGIN_BETTERCONTACT = "bettercontact"
ORIGIN_PROFILE_INFO = "profile_info"

def resolve(lead) -> str | None:
    """Always return None to bypass the OpenOutreach central cache."""
    return None

def contribute(session, lead, emails: list[str], origin: str) -> None:
    """No-op to prevent uploading lead data to any central store."""
    return
