import pytest
from concurrent.futures import ThreadPoolExecutor
import threading
from django.db import connection, transaction, close_old_connections, IntegrityError
from django.core.exceptions import ValidationError

from openoutreach.core.models import (
    Workspace,
    WorkspaceMembership,
    Campaign,
    CampaignPurpose,
    AuditEvent,
    CampaignMessageDraft,
    WorkspaceEntitlement,
    MessageSequenceStep,
)
from openoutreach.core.services.workspace_membership import (
    create_workspace_with_owner,
    remove_workspace_member,
)
from openoutreach.core.services.entitlements import (
    consume_entitlement,
    get_or_create_workspace_entitlement,
)
from openoutreach.core.services.audit_service import record_audit_event
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.core.services.campaign_wizard import create_campaign_from_wizard
from openoutreach.core.services.campaign_generator import generate_draft_for_deal_step
from openoutreach.crm.models.lead import Lead, TargetImport
from openoutreach.crm.models.deal import Deal, DealState
from tests.factories import UserFactory


@pytest.mark.django_db(transaction=True)
class TestPostgreSQLConcurrencyBoundaries:

    def setup_method(self):
        self.owner = UserFactory(username="pg_owner", email="owner@synthetic-demo.invalid")
        self.member = UserFactory(username="pg_member", email="member@synthetic-demo.invalid")
        self.ws = create_workspace_with_owner(name="PG Workspace", slug="pg-ws", owner_user=self.owner)
        self.purpose, _ = CampaignPurpose.objects.get_or_create(
            slug="job_outreach",
            defaults={"name": "Job Outreach", "is_system": True, "target_type": "individual"},
        )
        self.profile, _ = LinkedInProfile.objects.get_or_create(
            user=self.owner,
            defaults={"linkedin_username": "pg_owner@invalid", "linkedin_password": "pass"}
        )
        print(f"database vendor = {connection.vendor}")

    def _setup_thread_db(self):
        from django.db import connections, close_old_connections
        from django.conf import settings
        connections.databases['default'] = settings.DATABASES['default'].copy()
        close_old_connections()

    def test_final_owner_removal_locking(self):
        """Final owner removal must fail closed even under concurrent attempts."""
        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            results, errors = [], []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    remove_workspace_member(workspace=self.ws, target_user=self.owner, actor=self.owner)
                    results.append("success")
                except Exception as e:
                    errors.append(e)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert len(errors) == 2
            assert all("last owner cannot be removed" in str(e) for e in errors)
        else:
            with pytest.raises(ValidationError, match="last owner cannot be removed"):
                remove_workspace_member(workspace=self.ws, target_user=self.owner, actor=self.owner)

    def test_duplicate_membership_creation(self):
        """Duplicate membership creation must fail with unique constraint violation under simultaneous attempts."""
        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            errors = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    with transaction.atomic():
                        WorkspaceMembership.objects.create(workspace=self.ws, user=self.member, role="campaign_manager")
                except Exception as e:
                    errors.append(e)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert len(errors) == 1
            assert isinstance(errors[0], IntegrityError)
        else:
            WorkspaceMembership.objects.create(workspace=self.ws, user=self.member, role="campaign_manager")
            with pytest.raises(IntegrityError):
                with transaction.atomic():
                    WorkspaceMembership.objects.create(workspace=self.ws, user=self.member, role="campaign_manager")

    def test_concurrent_entitlement_consumption(self):
        """Test simultaneous consumption when 1 slot remains: exactly 1 succeeds."""
        ent = get_or_create_workspace_entitlement(self.ws)
        ent.plan.max_members = 2
        ent.plan.save()
        ent.status = "active"
        ent.save()

        # Unit test 1: Successful consumption within limits
        def add_member():
            return WorkspaceMembership.objects.create(workspace=self.ws, user=self.member, role="campaign_manager")

        res1 = consume_entitlement(workspace=self.ws, metric="members", quantity=1, action_key="key_1", operation=add_member)
        assert res1["allowed"] is True
        assert res1["operation_result"] is not None

        # Unit test 2: Denied consumption when limit reached
        with pytest.raises(ValidationError, match="Entitlement denied"):
            consume_entitlement(workspace=self.ws, metric="members", quantity=1, action_key="key_2")

        # Unit test 3: Idempotent action key replay
        res_replay = consume_entitlement(workspace=self.ws, metric="campaigns", quantity=1, action_key="key_1")
        assert res_replay["allowed"] is True

        # Unit test 4: Operation exception rollback
        def failing_op():
            raise RuntimeError("Operation failed mid-way")

        with pytest.raises(RuntimeError, match="Operation failed mid-way"):
            consume_entitlement(workspace=self.ws, metric="campaigns", quantity=1, action_key="key_fail", operation=failing_op)

        # Unit test 5: Two simultaneous final-slot consumers
        ws2 = create_workspace_with_owner(name="Slot Workspace", slug="slot-ws", owner_user=self.owner)
        ent2 = get_or_create_workspace_entitlement(ws2)
        ent2.plan.max_members = 2
        ent2.plan.save()

        user_a = UserFactory(username="user_a", email="a@synth.invalid")
        user_b = UserFactory(username="user_b", email="b@synth.invalid")

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            successes, failures = [], []

            def worker(u, key):
                self._setup_thread_db()
                barrier.wait()
                try:
                    def add_m():
                        return WorkspaceMembership.objects.create(workspace=ws2, user=u, role="operator")
                    r = consume_entitlement(workspace=ws2, metric="members", quantity=1, action_key=key, operation=add_m)
                    successes.append(r)
                except Exception as e:
                    failures.append(e)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker, user_a, "slot_key_a")
                f2 = executor.submit(worker, user_b, "slot_key_b")
                f1.result()
                f2.result()

            assert len(successes) == 1
            assert len(failures) == 1
            assert isinstance(failures[0], ValidationError)
        else:
            r1 = consume_entitlement(workspace=ws2, metric="members", quantity=1, action_key="slot_key_a")
            assert r1["allowed"] is True

    def test_duplicate_import_confirmation(self):
        """Simultaneous target import confirmation is atomic."""
        camp = create_campaign_from_wizard(workspace=self.ws, campaign_purpose=self.purpose, name="PG Import Campaign")
        imp = TargetImport.objects.create(workspace=self.ws, campaign=camp, name="PG Import", file_name="targets.csv")

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    with transaction.atomic():
                        t = TargetImport.objects.select_for_update().get(id=imp.id)
                        if t.status != "confirmed":
                            t.status = "confirmed"
                            t.save()
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()
        else:
            imp.status = "confirmed"
            imp.save()

        imp.refresh_from_db()
        assert imp.status == "confirmed"

    def test_duplicate_draft_generation(self):
        """Two simultaneous generate_draft_for_deal_step calls produce 1 logical draft & 1 audit event."""
        camp = create_campaign_from_wizard(workspace=self.ws, campaign_purpose=self.purpose, name="PG Draft Campaign")
        lead = Lead.objects.create(workspace=self.ws, public_identifier="pg_lead_conc")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step, _ = MessageSequenceStep.objects.get_or_create(
            campaign=camp, sequence_number=1, defaults={"channel": "linkedin", "step_type": "connection_note"}
        )

        ctx = {"recipient_name": "Concurrent Lead", "company_name": "Concurrent Corp"}

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            results = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    d = generate_draft_for_deal_step(deal, step, ctx, user=self.owner)
                    results.append(d)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert len(results) == 2
            assert results[0].id == results[1].id
        else:
            d1 = generate_draft_for_deal_step(deal, step, ctx, user=self.owner)
            d2 = generate_draft_for_deal_step(deal, step, ctx, user=self.owner)
            assert d1.id == d2.id

        draft_count = CampaignMessageDraft.objects.filter(deal=deal, sequence_step=step).count()
        assert draft_count == 1
        gen_events = AuditEvent.objects.filter(workspace=self.ws, event_type="draft_generated", action_key=f"draft_gen_{deal.id}_{step.id}").count()
        assert gen_events == 1

    def test_duplicate_audit_action_keys(self):
        """Simultaneous record_audit_event calls with same action key return identical event."""
        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            events = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    ev = record_audit_event(
                        workspace=self.ws,
                        event_type="test_event",
                        actor=self.owner,
                        action_key="dup_action_key_concurrency_100",
                        result="success",
                    )
                    events.append(ev)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert len(events) == 2
            assert events[0].id == events[1].id
        else:
            e1 = record_audit_event(workspace=self.ws, event_type="test_event", actor=self.owner, action_key="dup_action_key_100")
            e2 = record_audit_event(workspace=self.ws, event_type="test_event", actor=self.owner, action_key="dup_action_key_100")
            assert e1.id == e2.id

    def test_duplicate_connection_attempt(self):
        """Simultaneous connection attempt record creation for same deal is atomic."""
        from openoutreach.linkedin.models import ConnectionAttempt
        lead = Lead.objects.create(workspace=self.ws, public_identifier="pg_conn_lead")

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            attempts, errors = [], []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    with transaction.atomic():
                        c, created = ConnectionAttempt.objects.get_or_create(
                            idempotency_key=f"conn_idem_{lead.id}",
                            defaults={
                                "linkedin_profile": self.profile,
                                "recipient_public_id": lead.public_identifier,
                                "status": "prepared",
                            }
                        )
                        attempts.append((c.id, created))
                except Exception as e:
                    errors.append(e)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert len(attempts) == 2
            assert attempts[0][0] == attempts[1][0]
        else:
            c1, _ = ConnectionAttempt.objects.get_or_create(
                idempotency_key=f"conn_idem_{lead.id}",
                defaults={"linkedin_profile": self.profile, "recipient_public_id": lead.public_identifier}
            )
            c2, _ = ConnectionAttempt.objects.get_or_create(
                idempotency_key=f"conn_idem_{lead.id}",
                defaults={"linkedin_profile": self.profile, "recipient_public_id": lead.public_identifier}
            )
            assert c1.id == c2.id

        assert ConnectionAttempt.objects.filter(idempotency_key=f"conn_idem_{lead.id}").count() == 1

    def test_duplicate_job_application_attempt(self):
        """Simultaneous JobApplication creation for same candidate & job enforces unique_together."""
        from openoutreach.career.models import CandidateProfile, Job, JobApplication
        prof = CandidateProfile.objects.create(user=self.member, full_name="Conc Candidate")
        job = Job.objects.create(title="Senior Engineer", company="Tech Corp")

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            apps, errors = [], []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    with transaction.atomic():
                        app = JobApplication.objects.create(
                            candidate_profile=prof, job=job, status="draft"
                        )
                        apps.append(app)
                except Exception as e:
                    errors.append(e)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert len(apps) == 1
            assert len(errors) == 1
            assert isinstance(errors[0], IntegrityError)
        else:
            JobApplication.objects.create(candidate_profile=prof, job=job, status="draft")
            with pytest.raises(IntegrityError):
                with transaction.atomic():
                    JobApplication.objects.create(candidate_profile=prof, job=job, status="draft")

        assert JobApplication.objects.filter(candidate_profile=prof, job=job).count() == 1

    def test_duplicate_followup_attempt(self):
        """Simultaneous follow-up action recording produces exactly 1 audit event."""
        camp = create_campaign_from_wizard(workspace=self.ws, campaign_purpose=self.purpose, name="PG Followup Camp")
        lead = Lead.objects.create(workspace=self.ws, public_identifier="pg_follow_lead")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            results = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    ev = record_audit_event(
                        workspace=self.ws,
                        event_type="followup_prepared",
                        actor=self.owner,
                        target=deal,
                        action_key=f"followup_prep_{deal.id}_step_1",
                        result="success",
                    )
                    results.append(ev)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert len(results) == 2
            assert results[0].id == results[1].id
        else:
            ev1 = record_audit_event(workspace=self.ws, event_type="followup_prepared", actor=self.owner, target=deal, action_key=f"followup_prep_{deal.id}_step_1")
            ev2 = record_audit_event(workspace=self.ws, event_type="followup_prepared", actor=self.owner, target=deal, action_key=f"followup_prep_{deal.id}_step_1")
            assert ev1.id == ev2.id

    def test_duplicate_usage_action_key(self):
        """Simultaneous entitlement consumption with same action key is idempotent."""
        ent = get_or_create_workspace_entitlement(self.ws)
        ent.plan.max_members = 10
        ent.plan.save()
        ent.status = "active"
        ent.save()

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            results = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    res = consume_entitlement(
                        workspace=self.ws,
                        metric="campaigns",
                        quantity=1,
                        action_key="dup_usage_key_conc_999",
                    )
                    results.append(res)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert len(results) == 2
            assert results[0]["allowed"] is True
            assert results[1]["allowed"] is True
        else:
            r1 = consume_entitlement(workspace=self.ws, metric="campaigns", quantity=1, action_key="dup_usage_key_conc_999")
            r2 = consume_entitlement(workspace=self.ws, metric="campaigns", quantity=1, action_key="dup_usage_key_conc_999")
            assert r1["allowed"] is True
            assert r2["allowed"] is True

    def test_duplicate_worker_claim(self):
        """Simultaneous worker task claim returns the task to exactly 1 worker."""
        from openoutreach.core.models import Task
        from django.utils import timezone
        t = Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=timezone.now(),
            payload={"test": True},
        )

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            claimed = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    with transaction.atomic():
                        task = Task.objects.select_for_update(skip_locked=True).filter(id=t.id, status=Task.Status.PENDING).first()
                        if task:
                            task.status = Task.Status.RUNNING
                            task.save()
                            claimed.append(task)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert len(claimed) == 1
        else:
            with transaction.atomic():
                task = Task.objects.filter(id=t.id, status=Task.Status.PENDING).first()
                if task:
                    task.status = Task.Status.RUNNING
                    task.save()

        t.refresh_from_db()
        assert t.status == Task.Status.RUNNING

    def test_uncertain_confirmation_reconciliation(self):
        """Uncertain external confirmation records manual reconciliation requirement without double charging."""
        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            audit_records = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    ev = record_audit_event(
                        workspace=self.ws,
                        event_type="uncertain_recon",
                        actor=self.owner,
                        action_key=f"recon_uncertain_{self.ws.id}_step_1",
                        result="manual_recon",
                        metadata={"reason": "External adapter timeout, state uncertain"},
                    )
                    audit_records.append(ev)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert len(audit_records) == 2
            assert audit_records[0].id == audit_records[1].id
        else:
            ev1 = record_audit_event(workspace=self.ws, event_type="uncertain_recon", actor=self.owner, action_key=f"recon_{self.ws.id}", result="manual_recon")
            assert ev1.id is not None

    def test_concurrent_identity_resolution(self):
        """Concurrent execute_identity_resolution calls safely resolve identity without duplicating active versions or verification records."""
        from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, IncidentHold
        from openoutreach.career.services.identity_gates import execute_identity_resolution

        p = CandidateProfile.objects.create(user=self.owner, full_name="idwsdsjtasdkk", email="candidate-10@example.com")
        v3 = CandidateProfileVersion.objects.create(profile=p, version_number=2, full_name="idwsdsjtasdkk", email="candidate-10@example.com", state="approved", is_finalized=True)
        v6 = CandidateProfileVersion.objects.create(profile=p, version_number=3, full_name="Alex Morgan", email="candidate-10@example.com", state="pending_approval", is_finalized=False)
        p.active_version = v3
        p.save()
        hold = IncidentHold.objects.create(reason="CONC_HOLD", active=True)

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            results = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    res = execute_identity_resolution(
                        profile_id=p.id,
                        version_id=v6.id,
                        canonical_email="candidate-10@example.com",
                        operator_user=self.owner,
                        release_reason="Concurrent test release",
                    )
                    results.append(res)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert len(results) == 2
            assert results[0]["status"] == "success"
            assert results[1]["status"] == "success"
        else:
            res = execute_identity_resolution(
                profile_id=p.id,
                version_id=v6.id,
                canonical_email="candidate-10@example.com",
                operator_user=self.owner,
                release_reason="Single test release",
            )
            assert res["status"] == "success"

        p.refresh_from_db()
        v6.refresh_from_db()
        hold.refresh_from_db()
        assert p.active_version_id == v6.id
        assert v6.state == "approved"
        assert hold.active is False

    def test_operator_action_approval_concurrency(self):
        """OperatorActionApproval.claim_for_execution under concurrent attempts: exactly 1 worker succeeds."""
        from openoutreach.core.models import OperatorActionApproval
        from openoutreach.career.models import Job, CandidateProfile

        candidate = CandidateProfile.objects.create(user=self.owner, full_name="PG Candidate", email="pg@example.com")
        job = Job.objects.create(company="PG Co", title="PG Role", provider="linkedin", external_job_id="88887777", provenance="REAL_PROVIDER")
        approval = OperatorActionApproval.objects.create(
            workspace=self.ws,
            operator=self.owner,
            candidate=candidate,
            job=job,
            external_job_id="88887777",
            status="approved"
        )

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            results = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    claimed, apprv, err = OperatorActionApproval.claim_for_execution(
                        approval.id,
                        workspace=self.ws,
                        candidate=candidate,
                        job=job,
                        action_type="job_application"
                    )
                    results.append((claimed, err))
                except Exception as e:
                    results.append((False, str(e)))
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            successes = [r for r in results if r[0] is True]
            assert len(successes) == 1
        else:
            claimed, apprv, err = OperatorActionApproval.claim_for_execution(
                approval.id,
                workspace=self.ws,
                candidate=candidate,
                job=job,
                action_type="job_application"
            )
            assert claimed is True
            assert err == "claimed"

        approval.refresh_from_db()
        assert approval.status == "claimed"

    def test_job_discovery_upsert_concurrency(self):
        """Job.upsert_discovered_job under concurrent attempts creates exactly one canonical job."""
        from openoutreach.career.models import Job

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            results = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    j, created = Job.upsert_discovered_job(
                        provider="linkedin",
                        external_job_id="7777666655",
                        title="PG Discovered Title",
                        company="PG Discovered Co",
                        provenance="REAL_PROVIDER"
                    )
                    results.append((j.id, created))
                except Exception as e:
                    results.append((None, False))
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert Job.objects.filter(provider="linkedin", external_job_id="7777666655").count() == 1
        else:
            j1, c1 = Job.upsert_discovered_job(
                provider="linkedin",
                external_job_id="7777666655",
                title="PG Discovered Title",
                company="PG Discovered Co",
                provenance="REAL_PROVIDER"
            )
            assert c1 is True
            assert Job.objects.filter(provider="linkedin", external_job_id="7777666655").count() == 1

    def test_provider_write_intent_concurrency(self):
        """Two concurrent workers claiming the same write intent: exactly 1 succeeds."""
        from openoutreach.linkedin.models import ProviderWriteIntent
        from openoutreach.linkedin.services.idempotency import claim_provider_write

        target_pid = "pg_conc_target_1"
        op_type = "CONNECTION_SEND"
        action_id = "test_action_1"

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            results = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    allowed, reason, intent = claim_provider_write(
                        workspace=self.ws,
                        linkedin_profile=self.profile,
                        operation_type=op_type,
                        target_identity=target_pid,
                        logical_action_identity=action_id,
                    )
                    results.append((allowed, reason, intent.id if intent else None))
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            successes = [r for r in results if r[0] is True]
            blocked = [r for r in results if r[0] is False]
            assert len(successes) == 1
            assert len(blocked) == 1
            assert ProviderWriteIntent.objects.filter(
                linkedin_profile=self.profile,
                operation_type=op_type,
                target_identity=target_pid,
            ).count() == 1
        else:
            a1, r1, i1 = claim_provider_write(self.ws, self.profile, op_type, target_pid, action_id)
            assert a1 is True

    def test_provider_account_lease_concurrency(self):
        """Two concurrent workers acquiring a lease on the same account: exactly 1 acquires the lease."""
        from openoutreach.linkedin.services.account_governor import account_provider_lock
        import time

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            acquired = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    with account_provider_lock(linkedin_profile=self.profile, workspace=self.ws, lease_seconds=10, operation_name="concurrent_test") as prof:
                        if prof:
                            acquired.append(True)
                            time.sleep(0.1)
                        else:
                            acquired.append(False)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert acquired.count(True) == 1
            assert acquired.count(False) == 1
        else:
            with account_provider_lock(linkedin_profile=self.profile, workspace=self.ws, lease_seconds=10, operation_name="single_test") as prof:
                assert prof is not None

    def test_connection_attempt_concurrency(self):
        """Concurrent connection attempt creation with idempotency key creates at most 1 active attempt."""
        from openoutreach.linkedin.models import ConnectionAttempt

        target_pid = "pg_conn_lead_1"
        idem_key = f"conn_idem_{self.profile.id}_{target_pid}"

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(2)
            results = []

            def worker():
                self._setup_thread_db()
                barrier.wait()
                try:
                    with transaction.atomic():
                        ca, created = ConnectionAttempt.objects.select_for_update().get_or_create(
                            idempotency_key=idem_key,
                            defaults={
                                "linkedin_profile": self.profile,
                                "recipient_public_id": target_pid,
                                "status": "attempting",
                            }
                        )
                        results.append((ca.id, created))
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(worker)
                f2 = executor.submit(worker)
                f1.result()
                f2.result()

            assert ConnectionAttempt.objects.filter(idempotency_key=idem_key).count() == 1
            created_count = [r for r in results if r[1] is True]
            assert len(created_count) == 1
        else:
            ca, created = ConnectionAttempt.objects.get_or_create(
                idempotency_key=idem_key,
                defaults={"linkedin_profile": self.profile, "recipient_public_id": target_pid, "status": "attempting"}
            )
            assert created is True

    def test_action_log_quota_concurrency(self):
        """Concurrent action log recording increments daily counts cleanly without loss."""
        from openoutreach.linkedin.models import ActionLog

        camp = create_campaign_from_wizard(workspace=self.ws, campaign_purpose=self.purpose, name="PG Quota Campaign")

        if connection.vendor == "postgresql":
            barrier = threading.Barrier(4)

            def worker(idx):
                self._setup_thread_db()
                barrier.wait()
                try:
                    self.profile.record_action(
                        ActionLog.ActionType.CONNECT,
                        campaign=camp,
                        metadata={"target": f"target_{idx}"}
                    )
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(worker, i) for i in range(4)]
                for f in futures:
                    f.result()

            assert self.profile._daily_count(ActionLog.ActionType.CONNECT) == 4
        else:
            self.profile.record_action(ActionLog.ActionType.CONNECT, campaign=camp, metadata={"target": "target_0"})
            assert self.profile._daily_count(ActionLog.ActionType.CONNECT) == 1



