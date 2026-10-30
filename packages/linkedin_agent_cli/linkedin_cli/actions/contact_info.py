# linkedin/actions/contact_info.py
import logging

from ..api.client import PlaywrightLinkedinAPI

logger = logging.getLogger(__name__)


def get_contact_info(session, profile: dict):
    """Fetch a member's contact details (email, phone) via the profile overlay."""
    public_id = profile["public_identifier"]

    session.ensure_browser()
    session.wait()

    api = PlaywrightLinkedinAPI(session=session)
    logger.info("Fetching contact info → %s", public_id)
    contact, raw = api.get_contact_info(public_id)
    logger.info("Contact info – %s (email=%s)", public_id, bool(contact.get("email")))

    return contact, raw
