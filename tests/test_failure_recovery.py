"""
Tests for Failure Injection and Recovery against production service.
"""
import pytest
from openoutreach.core.models import Workspace, AuditEvent
from openoutreach.core.services.failure_recovery import classify_failure, handle_service_failure


@pytest.mark.django_db
class TestFailureRecovery:

    @pytest.fixture(autouse=True)
    def setup_data(self, db, django_user_model):
        self.User = django_user_model
        self.user = self.User.objects.create_user(username="fail_owner", email="fail@example.com")
        self.ws = Workspace.objects.create(name="Fail Workspace", slug="fail-ws")

    def test_non_retriable_captcha(self):
        category, retriable, desc = classify_failure("CAPTCHA / Security Challenge detected")
        assert retriable is False
        assert category == "captcha_detected"

    def test_non_retriable_platform_warning(self):
        category, retriable, desc = classify_failure("Platform account warning active")
        assert retriable is False
        assert category == "platform_warning"

    def test_non_retriable_identity_failure(self):
        category, retriable, desc = classify_failure("Identity verification unverified")
        assert retriable is False
        assert category == "identity_failure"

    def test_retriable_network_failure(self):
        category, retriable, desc = classify_failure("Network socket connection timeout")
        assert retriable is True
        assert category == "network_timeout"

    def test_retriable_selector_failure(self):
        category, retriable, desc = classify_failure("DOM selector element not found")
        assert retriable is True
        assert category == "selector_mismatch"

    def test_audit_event_created_on_failure(self):
        info = handle_service_failure(
            workspace=self.ws,
            action_key="act_6",
            error="Browser process crashed unexpectedly",
            attempt_count=1
        )
        assert AuditEvent.objects.filter(workspace=self.ws, event_type="execution_failed").exists()
        assert info["retriable"] is True
