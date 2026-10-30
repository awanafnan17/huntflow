# tests/test_linkedin_session_preservation.py
import pytest
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.browser.session import AccountSession
from linkedin_cli.exceptions import AuthenticationError

@pytest.mark.django_db
class TestLinkedInSessionPreservation:
    @pytest.fixture(autouse=True)
    def setup_data(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user(username="sessionuser", email="user@example.com")
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            linkedin_username="user@example.com",
            linkedin_password="test-2-aaaaaaaa",
            cookie_data={"cookies": [{"name": "li_at", "value": "testcookie"}]},
            session_status="valid"
        )

    def test_api_401_does_not_clear_cookie_data(self):
        """Verify that an API 401 Voyager error sets session_status='expired' without setting cookie_data=None."""
        sess = AccountSession(self.profile)

        # Simulate 401 handling inside self_profile
        from unittest.mock import patch
        with patch("linkedin_cli.setup.self_profile.discover_self_profile", side_effect=AuthenticationError("401 Unauthorized")), \
             patch("openoutreach.linkedin.db.leads.register_self_lead"):
            profile_dict = sess.self_profile

        self.profile.refresh_from_db()
        assert self.profile.cookie_data is not None
        c_data = self.profile.get_decrypted_cookie_data() if hasattr(self.profile, "get_decrypted_cookie_data") else self.profile.cookie_data
        assert (c_data or self.profile.cookie_data)["cookies"][0]["value"] == "testcookie"
        assert self.profile.session_status == "expired"
        assert "401" in self.profile.session_error_details

    def test_checkpoint_status_preserves_cookie_data(self):
        """Verify that setting session_status='checkpoint' keeps stored cookies intact."""
        self.profile.session_status = "checkpoint"
        self.profile.session_error_details = "Security Verification"
        self.profile.save()

        self.profile.refresh_from_db()
        assert self.profile.cookie_data is not None
        assert self.profile.session_status == "checkpoint"
