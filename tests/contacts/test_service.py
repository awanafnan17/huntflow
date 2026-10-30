# tests/contacts/test_service.py
"""Test stub for HuntFlow's deactivated contacts service."""
import pytest
from unittest.mock import MagicMock
from openoutreach.contacts import service
from tests.factories import LeadFactory


@pytest.mark.django_db
class TestResolve:
    def test_resolve_always_returns_none(self):
        lead = LeadFactory(public_identifier="jane-doe")
        assert service.resolve(lead) is None


@pytest.mark.django_db
class TestContribute:
    def test_contribute_always_noop(self):
        lead = LeadFactory(country_code="in")
        session = MagicMock()
        # Should not raise any errors or try to make network requests
        service.contribute(session, lead, ["jane@acme.com"], "bettercontact")
