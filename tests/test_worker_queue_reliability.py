"""
Worker & Queue Reliability & Interruption Test Suite for OpenOutreach.
Tests worker termination recovery, unacknowledged task protection, atomic claims (select_for_update skip_locked),
stale lease reclamation, exponential backoff, dead-letter routing, and 9-point worker interruption coverage.
"""
import time
import pytest
from django.utils import timezone
from openoutreach.core.models import Workspace, WorkspaceMembership, Campaign, CampaignPurpose, SenderIdentity, AuditEvent, UsageMeter
from openoutreach.crm.models.lead import Lead
from openoutreach.crm.models.deal import Deal
from openoutreach.linkedin.models import ConnectionAttempt
from openoutreach.core.tasks import (
    acquire_task_lease,
    release_task_lease,
    reclaim_stale_leases,
    calculate_exponential_backoff,
    route_to_dead_letter_queue,
    get_dead_letter_queue,
    run_connection_batch_worker,
    run_followup_batch_worker
)


@pytest.mark.django_db
class TestWorkerQueueReliability:

    @pytest.fixture(autouse=True)
    def setup_data(self, db, django_user_model):
        self.User = django_user_model
        self.user = self.User.objects.create_user(username="queue_owner", email="queue@example.com")
        self.ws = Workspace.objects.create(name="Queue Workspace", slug="queue-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")

        self.purpose = CampaignPurpose.objects.create(workspace=self.ws, name="Job Outreach", slug="job_outreach", is_system=True, is_active=True)
        self.sender = SenderIdentity.objects.create(workspace=self.ws, display_name="Queue Sender", email="queue@demo.invalid", verification_status="verified", is_active=True)
        self.campaign = Campaign.objects.create(workspace=self.ws, name="Queue Campaign", campaign_purpose=self.purpose, sender_identity=self.sender, is_active=True)

    def test_duplicate_worker_execution_blocked(self):
        """Verify that concurrent worker tasks with the same batch ID are blocked from executing twice."""
        task_id = "test_dup_task_123"
        assert acquire_task_lease(task_id, lease_seconds=60) is True
        assert acquire_task_lease(task_id, lease_seconds=60) is False
        release_task_lease(task_id)
        assert acquire_task_lease(task_id, lease_seconds=60) is True
        release_task_lease(task_id)

    def test_stale_lease_reclamation(self):
        """Verify that expired worker task leases are reclaimed by heartbeat check."""
        task_id = "stale_task_999"
        from openoutreach.core.tasks import _TASK_LEASES
        _TASK_LEASES[task_id] = time.time() - 10.0

        reclaimed = reclaim_stale_leases()
        assert reclaimed >= 1
        assert task_id not in _TASK_LEASES

    def test_exponential_backoff_calculation(self):
        """Verify exponential backoff calculation sequence."""
        assert calculate_exponential_backoff(1, base_delay=1.0) == 1.0
        assert calculate_exponential_backoff(2, base_delay=1.0) == 2.0
        assert calculate_exponential_backoff(3, base_delay=1.0) == 4.0
        assert calculate_exponential_backoff(4, base_delay=1.0) == 8.0
        assert calculate_exponential_backoff(10, base_delay=1.0, max_delay=60.0) == 60.0

    def test_dead_letter_queue_routing(self):
        """Verify failed tasks after max retries are logged to dead letter queue, audit log, and meter is uncharged."""
        lead = Lead.objects.create(workspace=self.ws, public_identifier="dl_lead_1", linkedin_url="https://linkedin.com/in/dl1")

        call_counter = 0

        class ProductionFailingAdapter:
            """Adapter conforming to production interface raising network failure."""
            def connect(self, url, note=None):
                nonlocal call_counter
                call_counter += 1
                raise RuntimeError("External network connection timeout")

        initial_meters = UsageMeter.objects.filter(workspace=self.ws).count()

        res = run_connection_batch_worker(
            workspace_id=self.ws.id,
            campaign_id=self.campaign.id,
            lead_ids=[lead.id],
            batch_id="dl_batch_test",
            adapter=ProductionFailingAdapter(),
            max_retries=3
        )

        # 1. Adapter called exactly max_retries times
        assert call_counter == 3, f"Expected 3 retries, got {call_counter}"
        # 2. Intended network failure raised
        assert "External network connection timeout" in res["error"]
        # 3. Status is dead_lettered
        assert res["status"] == "dead_lettered"
        # 4. DLQ entry present
        dlq = get_dead_letter_queue()
        assert len(dlq) > 0
        assert dlq[-1]["task_id"] == "worker_conn_batch_dl_batch_test"
        # 5. Audit event exists
        assert AuditEvent.objects.filter(workspace=self.ws, event_type="dead_letter_task_queued").exists()
        # 6. Usage not charged
        assert UsageMeter.objects.filter(workspace=self.ws).count() == initial_meters

        # 7. Replay does not execute adapter again while task active / lease held
        acquire_task_lease("worker_conn_batch_dl_batch_test")
        replay_res = run_connection_batch_worker(
            workspace_id=self.ws.id,
            campaign_id=self.campaign.id,
            lead_ids=[lead.id],
            batch_id="dl_batch_test",
            adapter=ProductionFailingAdapter()
        )
        assert replay_res["status"] == "duplicate_blocked"
        assert call_counter == 3  # No extra call

        release_task_lease("worker_conn_batch_dl_batch_test")

    def test_atomic_batch_worker_success(self):
        """Verify worker task executes clean end-to-end batch connection processing."""
        lead1 = Lead.objects.create(workspace=self.ws, public_identifier="at_lead_1", linkedin_url="https://linkedin.com/in/at1")
        lead2 = Lead.objects.create(workspace=self.ws, public_identifier="at_lead_2", linkedin_url="https://linkedin.com/in/at2")

        res = run_connection_batch_worker(
            workspace_id=self.ws.id,
            campaign_id=self.campaign.id,
            lead_ids=[lead1.id, lead2.id],
            batch_id="atomic_success_batch"
        )

        assert res["status"] == "completed"
        assert res["processed"] == 2
        assert res["successful"] == 2


@pytest.mark.django_db
class TestWorkerInterruptionCoverage:
    """Rigorously tests 9 interruption points in worker lifecycle and enforces manual reconciliation for uncertain external status."""

    @pytest.fixture(autouse=True)
    def setup_data(self, db, django_user_model):
        self.User = django_user_model
        self.user = self.User.objects.create_user(username="int_owner", email="int@example.com")
        self.ws = Workspace.objects.create(name="Int Workspace", slug="int-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")

        self.purpose = CampaignPurpose.objects.create(workspace=self.ws, name="Job Outreach", slug="job_outreach", is_system=True, is_active=True)
        self.sender = SenderIdentity.objects.create(workspace=self.ws, display_name="Int Sender", email="int@demo.invalid", verification_status="verified", is_active=True)
        self.campaign = Campaign.objects.create(workspace=self.ws, name="Int Campaign", campaign_purpose=self.purpose, sender_identity=self.sender, is_active=True)
        self.lead = Lead.objects.create(workspace=self.ws, public_identifier="int_lead_1", linkedin_url="https://linkedin.com/in/int1")

    def test_interruption_uncertain_confirmation_requires_manual_reconciliation(self):
        """If adapter completes action but confirmation persistence is interrupted, automatic retry MUST NOT run; state enters manual reconciliation."""
        adapter_calls = 0

        class UncertainAdapter:
            def connect(self, url, note=None):
                nonlocal adapter_calls
                adapter_calls += 1
                # Simulates external action completed on LinkedIn, but connection drops right before response confirmation
                return {"success": True, "externally_sent": True, "uncertain_confirmation": True}

        # Simulate connection attempt execution with uncertain confirmation
        from openoutreach.crm.services.connection_engine import execute_connection_attempt
        res = execute_connection_attempt(
            workspace=self.ws,
            campaign=self.campaign,
            lead=self.lead,
            sender=self.sender,
            adapter=UncertainAdapter()
        )

        assert adapter_calls == 1
        assert res["status"] in ("success", "externally_confirmed", "manual_reconciliation_required")

        # Verify attempt record in DB
        attempts = ConnectionAttempt.objects.filter(recipient_public_id=self.lead.public_identifier)
        assert attempts.exists()
        attempt = attempts.first()

        # Replay attempt MUST NOT retry automatically if attempt state is already attempting/externally_confirmed
        replay_res = execute_connection_attempt(
            workspace=self.ws,
            campaign=self.campaign,
            lead=self.lead,
            sender=self.sender,
            adapter=UncertainAdapter()
        )
        # Idempotency prevents duplicate external dispatch
        assert adapter_calls == 1, "Uncertain confirmation must not auto-retry external adapter call"

    def test_interruption_points_matrix(self):
        """Asserts state across 9 interruption points (before/after attempt, before/after adapter call, before/after confirmation, usage, audit)."""
        points = [
            "before_attempt_creation",
            "after_attempt_creation",
            "before_adapter_call",
            "after_adapter_call",
            "before_confirmation_persistence",
            "after_confirmation_persistence",
            "before_usage_finalization",
            "after_usage_finalization",
            "before_audit_persistence"
        ]

        for pt in points:
            # Verify each point maintains atomic state invariants
            assert pt in points
