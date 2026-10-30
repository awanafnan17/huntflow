import pytest
from django.contrib.auth.models import User
from openoutreach.core.models import Workspace, SenderIdentity, AuditEvent
from openoutreach.career.models import (
    CandidateProfile,
    CandidateProfileVersion,
    CandidateIdentityCorrectionLedger,
    CandidateIdentityVerification,
    IncidentHold,
)
from openoutreach.career.services.identity_gates import (
    get_real_action_eligibility,
    execute_identity_resolution,
)


@pytest.mark.django_db
def test_identity_resolution_workflow_and_durability():
    """Verify CandidateProfileVersion 6 approval, IncidentHold release, and canonical enablement persistence."""
    user = User.objects.create(username="operator_admin", is_superuser=True)
    ws = Workspace.objects.create(name="Res WS", slug="res-ws")
    user.workspace_memberships.create(workspace=ws, role="owner", status="active")

    cand_prof = CandidateProfile.objects.create(user=user, full_name="idwsdsjtasdkk", email="candidate-10@example.com")
    v3 = CandidateProfileVersion.objects.create(profile=cand_prof, version_number=2, full_name="idwsdsjtasdkk", email="candidate-10@example.com", state="approved", is_finalized=True)
    v6 = CandidateProfileVersion.objects.create(profile=cand_prof, version_number=3, full_name="Alex Morgan", email="candidate-10@example.com", state="pending_approval", is_finalized=False)

    cand_prof.active_version = v3
    cand_prof.save()

    hold = IncidentHold.objects.create(reason="IDENTITY_HOLD", active=True)

    # Initial eligibility check: should be BLOCKED by IncidentHold and unapproved version
    initial_elig = get_real_action_eligibility(candidate_profile=cand_prof)
    assert initial_elig["account_enabled"] is False
    assert initial_elig["reason_code"] == "active_incident_hold"

    # Execute identity resolution
    res = execute_identity_resolution(
        profile_id=cand_prof.id,
        version_id=v6.id,
        canonical_email="candidate-10@example.com",
        operator_user=user,
        release_reason="Operator verified live identity",
    )

    assert res["status"] == "success"

    # Re-fetch models and verify
    cand_prof.refresh_from_db()
    v3.refresh_from_db()
    v6.refresh_from_db()
    hold.refresh_from_db()

    assert cand_prof.active_version_id == v6.id
    assert v6.state == "approved"
    assert v6.is_finalized is True
    assert v3.state == "approved"  # Preserved unchanged
    assert hold.active is False

    # Check AuditEvent logs created
    audit_events = list(AuditEvent.objects.filter(workspace=ws).values_list("event_type", flat=True))
    assert "candidate_version_approved" in audit_events
    assert "canonical_identity_email_resolved" in audit_events
    assert "candidate_identity_verified" in audit_events
    assert "sender_identity_approved" in audit_events
    assert "incident_hold_released" in audit_events

    # Final eligibility check: should be ENABLED
    final_elig = get_real_action_eligibility(candidate_profile=cand_prof)
    assert final_elig["account_enabled"] is True
    assert final_elig["reason_code"] == "account_identity_verified"


@pytest.mark.django_db
def test_log_audit_event_atomic_savepoint():
    """Verify _log_audit_event uses savepoint atomicity to prevent breaking outer transaction on duplicate action key."""
    from openoutreach.career.services.identity_gates import _log_audit_event
    from django.db import transaction, IntegrityError

    user = User.objects.create(username="savepoint_operator")
    ws = Workspace.objects.create(name="Savepoint WS", slug="savepoint-ws")

    with transaction.atomic():
        ev1 = _log_audit_event(
            workspace=ws,
            event_type="test_event",
            actor=user,
            action_key="action_key_100",
            payload={"step": 1},
        )
        assert ev1.id is not None

        # Duplicate attempt inside same outer transaction
        ev2 = _log_audit_event(
            workspace=ws,
            event_type="test_event",
            actor=user,
            action_key="action_key_100",
            payload={"step": 2},
        )
        assert ev2.id == ev1.id
        assert ev2.payload["step"] == 1

        # Transaction remains unbroken and usable
        ws.name = "Savepoint WS Updated"
        ws.save()

    ws.refresh_from_db()
    assert ws.name == "Savepoint WS Updated"
    assert AuditEvent.objects.filter(workspace=ws, action_key="action_key_100").count() == 1

    # Verify unrelated IntegrityError (e.g. missing non-nullable FK) is re-raised
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _log_audit_event(
                workspace=None,  # Non-nullable FK violation
                event_type="test_event",
                actor=user,
                action_key="action_key_invalid",
                payload={},
            )

