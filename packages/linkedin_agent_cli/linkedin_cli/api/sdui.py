"""Parsers for LinkedIn's server-driven-UI (RSC) responses.

Some flagship-web surfaces — including the profile "Contact info" overlay —
are rendered via server-driven UI: a POST returns an RSC stream (React flight
format) rather than a Voyager JSON blob. These helpers pull the fields we care
about out of that text payload.
"""
import re

# Contact links in the payload surface as navigate actions, e.g.
#   "url":"mailto:alice@example.com"   /   "url":"tel:+1555..."
_MAILTO = re.compile(r"mailto:([^\"\\<>\s]+)")
_TEL = re.compile(r"tel:([^\"\\<>\s]+)")


def _unique(values: list[str]) -> list[str]:
    """De-duplicate while preserving first-seen order."""
    return list(dict.fromkeys(values))


def parse_contact_info(rsc_text: str) -> dict:
    """Extract contact details from a ProfileContactDetailsOverlay RSC payload.

    Returns ``{email, emails, phone_numbers}``. ``email`` is the first address
    found (or ``None``). Only fields the member exposes to your network appear —
    email is typically present only for 1st-degree connections.
    """
    emails = _unique(_MAILTO.findall(rsc_text))
    phone_numbers = _unique(_TEL.findall(rsc_text))
    return {
        "email": emails[0] if emails else None,
        "emails": emails,
        "phone_numbers": phone_numbers,
    }
