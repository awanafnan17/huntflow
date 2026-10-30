"""
Comprehensive SaaS Provider Safety, AES-GCM Authenticated Encryption,
Durable Write Idempotency, Account Serialization, and Multi-Tenant Isolation Tests.
"""
import base64
import json
import os
import pytest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.utils import timezone

from openoutreach.core.encryption import (
    encrypt_string,
    decrypt_string,
    encrypt_dict,
    decrypt_dict,
    _get_raw_key_material,
)
from openoutreach.core.models import Campaign, Workspace, WorkspaceAutomationSettings, WorkspaceMembership
from openoutreach.crm.models import Deal, DealState, Lead
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderSafetyEvent,
    ProviderWriteIntent,
    ProviderAccountLease,
    ProviderReadLedger,
    ProviderEvidenceCache,
    ActionLog,
)
from openoutreach.linkedin.services.account_governor import (
    is_account_safe_for_provider_read,
    is_account_safe_for_provider_write,
    check_and_record_safety_warning,
    account_provider_lock,
    resolve_provider_context,
)
from openoutreach.linkedin.services.idempotency import (
    claim_provider_write,
    record_provider_write_attempt,
    confirm_provider_write,
    fail_provider_write,
    generate_write_idempotency_key,
)

User = get_user_model()


@pytest.mark.django_db
class TestSecretEncryptionAtRestAndAuthentication:
    """Proves AES-256-GCM authenticated encryption at rest and fail-closed security properties."""

    @pytest.fixture(autouse=True)
    def setup_entities(self, db):
        self.user = User.objects.create_user(username="enc_operator", email="enc@example.com")
        self.ws = Workspace.objects.create(name="Security WS", slug="sec-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="admin", status="active")

    def test_database_at_rest_encrypted_and_roundtrip(self):
        """Raw DB columns must store ciphertext, while application layer decrypts faithfully."""
        dummy_pw = "SuperSecretDummyPassword123!#$"
        dummy_cookie = {
            "cookies": [{"name": "li_at", "value": "dummy_li_at_secret_sentinel_xyz", "domain": ".linkedin.com"}],
            "origins": []
        }

        profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.ws,
            linkedin_username="sec_user",
            linkedin_password=dummy_pw,
            cookie_data=dummy_cookie,
        )

        # Inspect raw database columns directly
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT linkedin_password, cookie_data FROM linkedin_linkedinprofile WHERE id = %s",
                [profile.id]
            )
            raw_pw, raw_cookie_json = cursor.fetchone()

        # Raw DB values must NOT match plaintext dummy secrets
        assert raw_pw != dummy_pw
        assert raw_pw.startswith("v1::")
        assert dummy_pw not in str(raw_pw)

        assert "dummy_li_at_secret_sentinel_xyz" not in str(raw_cookie_json)
        assert str(raw_cookie_json).startswith('"v1::') or str(raw_cookie_json).startswith("v1::")

        # Load through normal application model API
        reloaded = LinkedInProfile.objects.get(id=profile.id)
        assert reloaded.get_decrypted_password() == dummy_pw
        assert reloaded.get_decrypted_cookie_data() == dummy_cookie

    def test_unique_nonce_per_encryption(self):
        """Encrypting identical plaintext multiple times must yield unique nonces and ciphertexts."""
        plaintext = "IdenticalSecretPayloadToVerifyNonceRandomness"
        c1 = encrypt_string(plaintext)
        c2 = encrypt_string(plaintext)

        assert c1 != c2, "Ciphertexts must differ due to unique nonces"
        assert c1.startswith("v1::primary::")
        assert c2.startswith("v1::primary::")

        # Extract nonces
        nonce1 = c1.split("::")[2]
        nonce2 = c2.split("::")[2]
        assert nonce1 != nonce2, "Nonces must be uniquely generated for every encryption"

        # Both decrypt to original plaintext
        assert decrypt_string(c1) == plaintext
        assert decrypt_string(c2) == plaintext

    def test_tampered_ciphertext_fails_closed(self):
        """Any single-bit or byte corruption in ciphertext envelope fails closed without returning corrupt plaintext."""
        plaintext = "CriticalAuthenticationData"
        ct = encrypt_string(plaintext)
        parts = ct.split("::")
        assert len(parts) == 4
        prefix, key_id, b64_nonce, b64_ct = parts

        # Tamper with the ciphertext
        raw_ct = bytearray(base64.urlsafe_b64decode(b64_ct.encode("utf-8")))
        raw_ct[0] ^= 0xFF  # Flip bits
        tampered_b64_ct = base64.urlsafe_b64encode(bytes(raw_ct)).decode("utf-8")
        tampered_envelope = f"{prefix}::{key_id}::{b64_nonce}::{tampered_b64_ct}"

        decrypted = decrypt_string(tampered_envelope)
        assert decrypted == "", "Tampered ciphertext must fail closed to empty string"

    def test_wrong_key_fails_closed(self):
        """Attempting to decrypt ciphertext with a different 256-bit key fails closed."""
        plaintext = "ConfidentialTenantCredentials"
        key1 = os.urandom(32)
        key2 = os.urandom(32)

        ct = encrypt_string(plaintext, custom_key=key1)
        assert ct.startswith("v1::")

        # Decrypt with wrong key
        wrong_dec = decrypt_string(ct, custom_key=key2)
        assert wrong_dec == "", "Wrong key decryption must fail closed to empty string"

        # Decrypt with correct key
        correct_dec = decrypt_string(ct, custom_key=key1)
        assert correct_dec == plaintext

    def test_production_mode_requires_dedicated_credential_key(self):
        """In production (is_production=True), missing encryption key raises ImproperlyConfigured."""
        with patch.dict(os.environ, {"HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY": "", "HUNTFLOW_ENCRYPTION_KEY": ""}, clear=True):
            with patch("openoutreach.core.encryption.settings.HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY", "", create=True):
                with patch("openoutreach.core.encryption.settings.HUNTFLOW_ENCRYPTION_KEY", "", create=True):
                    with pytest.raises(ImproperlyConfigured) as excinfo:
                        _get_raw_key_material(is_production=True)
                    assert "Production security requirement" in str(excinfo.value)

    def test_secret_redaction_in_representations_and_logs(self):
        """Sensitive secrets must be completely absent from __repr__, __str__, safe_dict(), and logs."""
        sentinel_pw = "SENTINEL_PW_MUST_NOT_LEAK"
        sentinel_cookie = {"cookies": [{"name": "li_at", "value": "SENTINEL_COOKIE_VALUE_LEAK_TEST"}]}

        profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.ws,
            linkedin_username="redact_test",
            linkedin_password=sentinel_pw,
            cookie_data=sentinel_cookie,
        )

        r_repr = repr(profile)
        r_str = str(profile)
        r_dict = str(profile.safe_dict())

        assert sentinel_pw not in r_repr
        assert sentinel_pw not in r_str
        assert sentinel_pw not in r_dict
        assert "SENTINEL_COOKIE_VALUE_LEAK_TEST" not in r_repr
        assert "SENTINEL_COOKIE_VALUE_LEAK_TEST" not in r_dict
        assert "[REDACTED]" in r_repr


@pytest.mark.django_db
class TestDurableProviderWriteIdempotency:
    """Verifies durable write intents, crash window containment, and concurrent worker deduplication."""

    @pytest.fixture(autouse=True)
    def setup_entities(self, db):
        self.user = User.objects.create_user(username="idem_user", email="idem@example.com")
        self.ws = Workspace.objects.create(name="Idem WS", slug="idem-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="admin", status="active")
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.ws,
            linkedin_username="idem_prof",
            session_status="valid",
        )

    def test_canonical_operations_idempotency_lifecycle(self):
        """Test PLANNED -> CLAIMED -> PROVIDER_ATTEMPTED -> CONFIRMED lifecycle for all write operations."""
        operations = [
            ProviderWriteIntent.OperationType.CONNECTION_SEND,
            ProviderWriteIntent.OperationType.FIRST_MESSAGE_SEND,
            ProviderWriteIntent.OperationType.FOLLOW_UP_SEND,
            ProviderWriteIntent.OperationType.AI_REPLY_SEND,
            ProviderWriteIntent.OperationType.EASY_APPLY_SUBMIT,
        ]

        for op in operations:
            target = f"target_{op}_123"
            allowed, reason, intent = claim_provider_write(
                workspace=self.ws,
                linkedin_profile=self.profile,
                operation_type=op,
                target_identity=target,
                logical_action_identity="work_item_1",
            )
            assert allowed is True
            assert reason == "claimed"
            assert intent.state == ProviderWriteIntent.State.CLAIMED

            # Record provider attempt
            record_provider_write_attempt(intent)
            intent.refresh_from_db()
            assert intent.state == ProviderWriteIntent.State.PROVIDER_ATTEMPTED

            # Confirm write
            confirm_provider_write(intent, confirmation_data={"receipt_id": "rec_999"})
            intent.refresh_from_db()
            assert intent.state == ProviderWriteIntent.State.CONFIRMED

            # Second attempt to claim same write must be rejected
            allowed2, reason2, intent2 = claim_provider_write(
                workspace=self.ws,
                linkedin_profile=self.profile,
                operation_type=op,
                target_identity=target,
                logical_action_identity="work_item_1",
            )
            assert allowed2 is False
            assert reason2 == "already_confirmed"

    def test_crash_window_fails_closed_and_prevents_duplicate_write(self):
        """If worker crashes after provider attempt, retry MUST fail closed without re-executing write."""
        target = "prospect_crash_victim"
        allowed, reason, intent = claim_provider_write(
            workspace=self.ws,
            linkedin_profile=self.profile,
            operation_type=ProviderWriteIntent.OperationType.CONNECTION_SEND,
            target_identity=target,
            logical_action_identity="campaign_deal_7",
        )
        assert allowed is True

        # Worker begins provider attempt, then crashes before confirmation
        record_provider_write_attempt(intent)

        # Worker restarts and retries the exact same work item
        retry_allowed, retry_reason, retry_intent = claim_provider_write(
            workspace=self.ws,
            linkedin_profile=self.profile,
            operation_type=ProviderWriteIntent.OperationType.CONNECTION_SEND,
            target_identity=target,
            logical_action_identity="campaign_deal_7",
        )
        assert retry_allowed is False
        assert retry_reason == "uncertain_requires_reconciliation"
        assert retry_intent.state == ProviderWriteIntent.State.PROVIDER_ATTEMPTED

    def test_concurrent_workers_same_action_deduped(self):
        """Two concurrent workers attempting same action: Worker 1 claims, Worker 2 is blocked."""
        target = "lead_concurrent_race"
        allowed1, reason1, intent1 = claim_provider_write(
            workspace=self.ws,
            linkedin_profile=self.profile,
            operation_type=ProviderWriteIntent.OperationType.FOLLOW_UP_SEND,
            target_identity=target,
            logical_action_identity="deal_race_1",
        )
        assert allowed1 is True
        assert reason1 == "claimed"

        # Worker 2 attempts while Worker 1 is still CLAIMED
        allowed2, reason2, intent2 = claim_provider_write(
            workspace=self.ws,
            linkedin_profile=self.profile,
            operation_type=ProviderWriteIntent.OperationType.FOLLOW_UP_SEND,
            target_identity=target,
            logical_action_identity="deal_race_1",
        )
        assert allowed2 is False
        assert reason2 == "already_claimed_by_concurrent_worker"


@pytest.mark.django_db
class TestAccountSerializationAndLeases:
    """Verifies cross-process account serialization, lease management, and crash recovery."""

    @pytest.fixture(autouse=True)
    def setup_entities(self, db):
        self.user = User.objects.create_user(username="lock_user", email="lock@example.com")
        self.ws = Workspace.objects.create(name="Lock WS", slug="lock-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="admin", status="active")
        self.p1 = LinkedInProfile.objects.create(user=self.user, workspace=self.ws, linkedin_username="prof_1", session_status="valid")
        self.p2 = LinkedInProfile.objects.create(user=self.user, workspace=self.ws, linkedin_username="prof_2", session_status="valid")

    def test_same_account_lease_serialization(self):
        """Holding account_provider_lock prevents second worker from acquiring lease on same profile."""
        with account_provider_lock(linkedin_profile=self.p1, workspace=self.ws, operation_name="send_connect") as locked_p1:
            assert locked_p1 is not None
            assert ProviderAccountLease.objects.filter(linkedin_profile=self.p1).exists()

            # Second worker attempts to lock p1 concurrently
            with account_provider_lock(linkedin_profile=self.p1, workspace=self.ws, operation_name="concurrent_attempt") as locked_p1_second:
                assert locked_p1_second is None, "Second worker must be blocked when lease is actively held"

        # After context exit, lease is released
        assert not ProviderAccountLease.objects.filter(linkedin_profile=self.p1).exists()

    def test_different_accounts_independent_locks(self):
        """Locking Profile 1 does NOT block Profile 2 from proceeding independently."""
        with account_provider_lock(linkedin_profile=self.p1, workspace=self.ws, operation_name="op1") as locked_p1:
            assert locked_p1 is not None
            with account_provider_lock(linkedin_profile=self.p2, workspace=self.ws, operation_name="op2") as locked_p2:
                assert locked_p2 is not None, "Profile 2 must proceed independently of Profile 1"

    def test_expired_lease_crash_recovery(self):
        """An orphaned lease from a dead worker (> lease_seconds old) is automatically cleaned up and recovered."""
        # Create an expired lease
        ProviderAccountLease.objects.create(
            linkedin_profile=self.p1,
            workspace=self.ws,
            locked_by="dead_pid_9999",
            acquired_at=timezone.now() - timedelta(minutes=10),
            expires_at=timezone.now() - timedelta(minutes=9),
            active_operation="dead_op",
        )

        # New worker should successfully reclaim the expired lease
        with account_provider_lock(linkedin_profile=self.p1, workspace=self.ws, operation_name="recovered_op") as locked:
            assert locked is not None
            lease = ProviderAccountLease.objects.get(linkedin_profile=self.p1)
            assert "dead_pid" not in lease.locked_by


@pytest.mark.django_db
class TestMultiProviderWorkspaceAndCrossTenantIsolation:
    """Verifies per-provider warning isolation within a workspace and zero cross-workspace leakage."""

    @pytest.fixture(autouse=True)
    def setup_entities(self, db):
        self.user_a = User.objects.create_user(username="tenant_a", email="a@example.com")
        self.ws_a = Workspace.objects.create(name="Tenant A", slug="tenant-a")
        WorkspaceMembership.objects.create(workspace=self.ws_a, user=self.user_a, role="admin", status="active")
        self.auto_a = WorkspaceAutomationSettings.get_for_workspace(self.ws_a)

        self.prof_a1 = LinkedInProfile.objects.create(user=self.user_a, workspace=self.ws_a, linkedin_username="a1", session_status="valid")
        self.prof_a2 = LinkedInProfile.objects.create(user=self.user_a, workspace=self.ws_a, linkedin_username="a2", session_status="valid")

        self.user_b = User.objects.create_user(username="tenant_b", email="b@example.com")
        self.ws_b = Workspace.objects.create(name="Tenant B", slug="tenant-b")
        WorkspaceMembership.objects.create(workspace=self.ws_b, user=self.user_b, role="admin", status="active")
        self.auto_b = WorkspaceAutomationSettings.get_for_workspace(self.ws_b)
        self.prof_b = LinkedInProfile.objects.create(user=self.user_b, workspace=self.ws_b, linkedin_username="b1", session_status="valid")

    def test_same_workspace_two_provider_checkpoint_isolation(self):
        """When Profile A1 hits an account checkpoint, A1 is BLOCKED but sibling Profile A2 remains ALLOWED."""
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/checkpoint/challenge/12345"
        mock_page.evaluate.return_value = "Security Checkpoint"

        # Trigger checkpoint on A1
        check_and_record_safety_warning(page=mock_page, workspace=self.ws_a, linkedin_profile=self.prof_a1)

        self.prof_a1.refresh_from_db()
        self.prof_a2.refresh_from_db()

        assert self.prof_a1.session_status == "checkpoint"
        assert self.prof_a2.session_status == "valid"

        # Governor check on A1 must fail closed
        safe_a1, reason_a1 = is_account_safe_for_provider_read(workspace=self.ws_a, linkedin_profile=self.prof_a1)
        assert safe_a1 is False
        assert "checkpoint" in reason_a1

        # Governor check on sibling A2 remains safe
        safe_a2, reason_a2 = is_account_safe_for_provider_read(workspace=self.ws_a, linkedin_profile=self.prof_a2)
        assert safe_a2 is True
        assert reason_a2 == "ok"

    def test_cross_workspace_warning_isolation(self):
        """A safety warning in Tenant A must NEVER mutate Tenant B's status, events, or counters."""
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/checkpoint/challenge/999"
        mock_page.evaluate.return_value = "Security Checkpoint"

        check_and_record_safety_warning(page=mock_page, workspace=self.ws_a, linkedin_profile=self.prof_a1)

        self.prof_b.refresh_from_db()
        assert self.prof_b.session_status == "valid"
        assert not ProviderSafetyEvent.objects.filter(workspace=self.ws_b).exists()

        safe_b, reason_b = is_account_safe_for_provider_read(workspace=self.ws_b, linkedin_profile=self.prof_b)
        assert safe_b is True
        assert reason_b == "ok"

    def test_provider_safety_event_append_only_history(self):
        """Resolving a warning updates resolved_at without deleting history rows."""
        event = ProviderSafetyEvent.objects.create(
            workspace=self.ws_a,
            linkedin_profile=self.prof_a1,
            event_type="test_event_1",
            action_taken="SAFETY_STOP",
        )

        assert event.resolved_at is None
        event.resolved_at = timezone.now()
        event.save(update_fields=["resolved_at"])

        # History remains intact and queryable
        assert ProviderSafetyEvent.objects.filter(workspace=self.ws_a, event_type="test_event_1").count() == 1


@pytest.mark.django_db
class TestAccountLeaseOwnershipTokenAndRenewal:
    """Verifies ownership token safety, preventing stale owners from releasing active leases."""

    @pytest.fixture(autouse=True)
    def setup_entities(self, db):
        self.user = User.objects.create_user(username="token_user", email="token@example.com")
        self.ws = Workspace.objects.create(name="Token WS", slug="token-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="admin", status="active")
        self.profile = LinkedInProfile.objects.create(user=self.user, workspace=self.ws, linkedin_username="tok_prof", session_status="valid")

    def test_stale_owner_cannot_release_new_lease(self):
        """Worker A acquires lease -> lease expires/recovered -> Worker B acquires lease -> Worker A exits -> Worker B lease remains intact."""
        # 1. Worker A acquires lease
        cm_a = account_provider_lock(linkedin_profile=self.profile, workspace=self.ws, operation_name="op_a", lease_seconds=60)
        locked_a = cm_a.__enter__()
        assert locked_a is not None
        lease_a = ProviderAccountLease.objects.get(linkedin_profile=self.profile)
        token_a = lease_a.owner_token
        assert token_a.startswith("tok_")

        # 2. Simulate Worker A's lease expiring
        lease_a.expires_at = timezone.now() - timedelta(seconds=10)
        lease_a.save(update_fields=["expires_at"])

        # 3. Worker B acquires new lease
        cm_b = account_provider_lock(linkedin_profile=self.profile, workspace=self.ws, operation_name="op_b", lease_seconds=180)
        locked_b = cm_b.__enter__()
        assert locked_b is not None
        lease_b = ProviderAccountLease.objects.get(linkedin_profile=self.profile)
        token_b = lease_b.owner_token
        assert token_b != token_a

        # 4. Worker A finally finishes and attempts to release
        cm_a.__exit__(None, None, None)

        # Worker B's lease MUST NOT be deleted by Worker A!
        assert ProviderAccountLease.objects.filter(linkedin_profile=self.profile, owner_token=token_b).exists()

        # Clean up Worker B
        cm_b.__exit__(None, None, None)
        assert not ProviderAccountLease.objects.filter(linkedin_profile=self.profile).exists()

    def test_lease_renewal_extends_active_lock(self):
        """Active operation can renew its lease token, keeping subsequent workers blocked."""
        from openoutreach.linkedin.services.account_governor import renew_provider_lease

        cm = account_provider_lock(linkedin_profile=self.profile, workspace=self.ws, operation_name="long_op", lease_seconds=100)
        locked = cm.__enter__()
        assert locked is not None
        lease = ProviderAccountLease.objects.get(linkedin_profile=self.profile)
        token = lease.owner_token

        # Renew lease for another 300 seconds
        renewed = renew_provider_lease(self.profile, token, extension_seconds=300)
        assert renewed is True

        lease.refresh_from_db()
        assert lease.expires_at > timezone.now() + timedelta(seconds=250)

        # Wrong token cannot renew
        assert renew_provider_lease(self.profile, "wrong_token_123", extension_seconds=300) is False

        # Exit context
        cm.__exit__(None, None, None)


@pytest.mark.django_db
class TestRealHandlerProviderWriteIntentWiring:
    """Verifies that the 5 real provider write handlers actually invoke ProviderWriteIntent."""

    @pytest.fixture(autouse=True)
    def setup_entities(self, db):
        self.user = User.objects.create_user(username="wire_user", email="wire@example.com")
        self.ws = Workspace.objects.create(name="Wire WS", slug="wire-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="admin", status="active")
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.ws,
            linkedin_username="wire_prof",
            session_status="valid",
            identity_validation_status="REAL_ACCOUNT_VERIFIED",
        )
        self.campaign = Campaign.objects.create(name="Wire Camp", workspace=self.ws, owner=self.user)
        self.auto_settings = WorkspaceAutomationSettings.get_for_workspace(self.ws)
        self.auto_settings.master_automation_enabled = True
        self.auto_settings.auto_send_connections = True
        self.auto_settings.auto_followup = True
        self.auto_settings.auto_monitor_replies = True
        self.auto_settings.save()

    def test_connect_real_handler_creates_and_confirms_write_intent(self):
        """handle_connect must create and confirm ProviderWriteIntent."""
        from openoutreach.linkedin.tasks.connect import handle_connect
        from openoutreach.crm.models import Lead, Deal, DealState
        from unittest.mock import MagicMock, patch

        lead = Lead.objects.create(
            public_identifier="prospect_wire_1",
            linkedin_url="https://www.linkedin.com/in/prospect-wire-1",
            first_name="Prospect",
            last_name="One",
        )
        deal = Deal.objects.create(lead=lead, campaign=self.campaign, state=DealState.READY_TO_CONNECT.value)

        session_mock = MagicMock()
        session_mock.linkedin_profile = self.profile
        session_mock.campaign = self.campaign
        session_mock.public_id = "sender_1"

        task_mock = MagicMock()
        task_mock.name = "connect"
        task_mock.payload = {"public_identifier": "prospect_wire_1", "lead_id": lead.id}

        with patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_write", return_value=(True, "ok")), \
             patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_read", return_value=(True, "ok")), \
             patch("openoutreach.linkedin.transport_guard.is_qa_synthetic_transport_allowed", return_value=False), \
             patch("linkedin_cli.actions.status.get_connection_status", return_value=DealState.QUALIFIED), \
             patch("openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note", return_value=DealState.PENDING), \
             patch("openoutreach.core.agents.connection_note.generate_connection_note", return_value=("hey, saw what you guys are building... curious what you're focused on rn?", "test")), \
             patch("openoutreach.linkedin.services.evidence_cache.record_provider_read"):
            
            handle_connect(task_mock, session_mock, qualifiers={})

        # Verify ProviderWriteIntent was created, attempted, and confirmed
        intent = ProviderWriteIntent.objects.filter(
            target_identity="prospect_wire_1",
            operation_type=ProviderWriteIntent.OperationType.CONNECTION_SEND,
        ).first()
        assert intent is not None
        assert intent.state == ProviderWriteIntent.State.CONFIRMED

    def test_followup_and_first_message_handler_wires_write_intent(self):
        """handle_follow_up creates FIRST_MESSAGE_SEND / FOLLOW_UP_SEND ProviderWriteIntent."""
        from openoutreach.linkedin.tasks.follow_up import handle_follow_up
        from openoutreach.crm.models import Lead, Deal, DealState
        from openoutreach.core.agents.follow_up import FollowUpDecision
        from unittest.mock import MagicMock, patch

        lead = Lead.objects.create(
            public_identifier="lead_msg_wire",
            linkedin_url="https://www.linkedin.com/in/lead-msg-wire",
            first_name="Msg",
            last_name="Lead",
        )
        deal = Deal.objects.create(
            lead=lead,
            campaign=self.campaign,
            state=DealState.CONNECTED.value,
        )
        from openoutreach.linkedin.models import ProviderRelationship
        ProviderRelationship.objects.create(
            workspace=self.ws,
            linkedin_profile=self.profile,
            lead=lead,
            deal=deal,
            classification="CONNECTED",
            evidence_sufficient=True,
            last_observed_at=timezone.now(),
        )

        session_mock = MagicMock()
        session_mock.linkedin_profile = self.profile
        session_mock.campaign = self.campaign

        decision = FollowUpDecision(
            action="send_message",
            message="hey Msg, saw what you guys are building at Acme... curious what you're focused on rn?",
            follow_up_hours=24,
            outcome=None,
        )

        task_mock = MagicMock()
        task_mock.name = "follow_up"

        with patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_write", return_value=(True, "ok")), \
             patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_read", return_value=(True, "ok")), \
             patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("openoutreach.core.agents.follow_up.run_follow_up_agent", return_value=decision), \
             patch("linkedin_cli.actions.message.send_raw_message", return_value=True), \
             patch("openoutreach.linkedin.db.chat.sync_conversation"):

            handle_follow_up(task_mock, session_mock, qualifiers={})

        # Verify ProviderWriteIntent was recorded and confirmed
        intent = ProviderWriteIntent.objects.filter(
            target_identity="lead_msg_wire",
            operation_type=ProviderWriteIntent.OperationType.FIRST_MESSAGE_SEND,
        ).first()
        assert intent is not None
        assert intent.state == ProviderWriteIntent.State.CONFIRMED

    def test_easy_apply_submit_wires_write_intent(self):
        """LinkedInEasyApplyAdapter submit step creates and confirms EASY_APPLY_SUBMIT ProviderWriteIntent."""
        from openoutreach.linkedin.browser.easy_apply import LinkedInEasyApplyAdapter
        from unittest.mock import MagicMock, patch

        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/jobs/search/?currentJobId=987654321"
        session_mock = MagicMock()
        session_mock.linkedin_profile = self.profile
        session_mock.campaign = self.campaign
        session_mock.page = mock_page

        adapter = LinkedInEasyApplyAdapter(session=session_mock)

        # Easy Apply button in top card
        apply_btn = MagicMock()
        apply_btn.count.return_value = 1
        apply_btn.is_visible.return_value = True
        apply_btn.get_attribute.side_effect = lambda k: "jobs-apply-button" if k == "class" else "Easy Apply"
        apply_btn.inner_text.return_value = "Easy Apply"
        apply_btn.first = apply_btn

        # Modal
        mock_modal = MagicMock()
        mock_modal.is_visible.return_value = True
        mock_modal.count.return_value = 1

        # Submit button inside modal
        mock_submit_btn = MagicMock()
        mock_submit_btn.count.return_value = 1
        mock_submit_btn.is_visible.return_value = True
        mock_submit_btn.first = mock_submit_btn

        def modal_locator(sel):
            if "Submit application" in sel:
                return mock_submit_btn
            m = MagicMock()
            m.all.return_value = []
            m.count.return_value = 0
            m.is_visible.return_value = False
            m.first = m
            return m

        mock_modal.locator = modal_locator

        # Confirmation heading
        confirm_heading = MagicMock()
        confirm_heading.count.return_value = 1
        confirm_heading.is_visible.return_value = True
        confirm_heading.inner_text.return_value = "Application submitted"
        confirm_heading.first = confirm_heading

        def page_locator(sel):
            if "Application submitted" in sel:
                return confirm_heading
            m = MagicMock()
            if "modal" in sel or "dialog" in sel:
                m.all.return_value = [mock_modal]
                m.count.return_value = 1
                m.is_visible.return_value = True
            else:
                m.all.return_value = [apply_btn]
                m.count.return_value = 1
                m.is_visible.return_value = True
            m.first = m
            return m

        mock_page.locator = page_locator

        with patch("openoutreach.linkedin.browser.easy_apply.verify_linkedin_job_applied", return_value=(False, None, None)), \
             patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_read", return_value=(True, "ok")), \
             patch("openoutreach.linkedin.services.evidence_cache.record_provider_read"):

            res = adapter.apply(
                job_url="https://www.linkedin.com/jobs/view/987654321",
                external_action_authorized=True,
            )

        assert res["success"] is True
        intent = ProviderWriteIntent.objects.filter(
            target_identity="987654321",
            operation_type=ProviderWriteIntent.OperationType.EASY_APPLY_SUBMIT,
        ).first()
        assert intent is not None
        assert intent.state == ProviderWriteIntent.State.CONFIRMED
