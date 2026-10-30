import pytest
import asyncio
from unittest.mock import MagicMock
from django.contrib.auth.models import User
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.crm.models import Lead
from asgiref.sync import sync_to_async
from openoutreach.linkedin.db.leads import register_self_lead, register_self_lead_sync


@pytest.mark.django_db(transaction=True)
def test_register_self_lead_async_context_no_crash():
    """Verify that calling register_self_lead inside an active asyncio event loop
    executes safely on a dedicated thread without raising SynchronousOnlyOperation,
    persists the self Lead, and propagates exceptions correctly."""
    user = User.objects.create_user(username="async_test_user")
    profile = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="test_self@example.com",
        linkedin_password="pass",
        active=True,
    )

    session = MagicMock()
    session.linkedin_profile = profile

    profile_data = {
        "public_identifier": "test-self-lead-123",
        "first_name": "Test",
        "last_name": "Self",
    }

    res_holder = [None]
    exc_holder = [None]

    def _thread_target():
        async def _async_caller():
            return register_self_lead(session, profile_data)
        loop = asyncio.new_event_loop()
        try:
            res_holder[0] = loop.run_until_complete(_async_caller())
        except Exception as e:
            exc_holder[0] = e
        finally:
            loop.close()

    import threading
    from django.db import connections
    connections.close_all()
    t = threading.Thread(target=_thread_target)
    t.start()
    t.join()

    if exc_holder[0] is not None:
        raise exc_holder[0]

    lead = res_holder[0]
    assert lead is not None
    assert lead.public_identifier == "test-self-lead-123"
    assert lead.disqualified is True
    assert Lead.objects.filter(public_identifier="test-self-lead-123").exists()

    profile.refresh_from_db()
    assert profile.self_lead == lead


@pytest.mark.django_db
def test_register_self_lead_exception_propagation():
    """Verify that DB exceptions occurring inside the thread propagate back to the async caller."""
    user = User.objects.create_user(username="exc_user")
    profile = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="exc@example.com",
        linkedin_password="pass",
        active=True,
    )

    session = MagicMock()
    session.linkedin_profile = profile

    invalid_profile_data = {}

    res_holder = [None]
    exc_holder = [None]

    def _thread_target():
        async def _async_caller():
            return register_self_lead(session, invalid_profile_data)
        loop = asyncio.new_event_loop()
        try:
            res_holder[0] = loop.run_until_complete(_async_caller())
        except Exception as e:
            exc_holder[0] = e
        finally:
            loop.close()

    import threading
    t = threading.Thread(target=_thread_target)
    t.start()
    t.join()

    assert exc_holder[0] is not None
    assert isinstance(exc_holder[0], KeyError)
