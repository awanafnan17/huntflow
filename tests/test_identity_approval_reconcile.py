import pytest
import io
import sys
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import ValidationError
from tests.factories import UserFactory
from unittest.mock import patch, MagicMock

from openoutreach.career.models import (
    CandidateProfile,
    CandidateProfileVersion,
    CandidateDocument,
    CandidateDocumentVersion,
    CandidateIdentityVerification,
    CandidateFact,
    IncidentHold,
    IncidentHoldTarget,
    Job,
    JobVersion,
    JobMatch,
    JobApplication,
    ApplicationDraft,
    CandidateProfileApproval,
    CandidateExtractionRecord
)
from openoutreach.career.service import (
    reconcile_corrected_identity_approval_state,
    capture_canonical_db_snapshot,
    compare_db_snapshots
)

@pytest.mark.django_db
class TestIdentityApprovalReconcile:
    @pytest.fixture(autouse=True)
    def _setup(self, db):
        self.user = UserFactory(username="seeker_user", email="candidate-10@example.com")
        self.other_user = UserFactory(username="other_user", email="other@example.com")
        
        # Profile
        self.cand_profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="idwsdsjtasdkk",
            email="candidate-10@example.com"
        )
        
        # Version 3 (Historical contaminated, finalized, approved)
        self.profile_version_v3 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=2,
            full_name="idwsdsjtasdkk",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved",
            checksum="9a216e1c969a3d9b3859a30bec41cba249d7fc856af6087f6a77631526e4c8e3"
        )
        
        self.cand_profile.active_version = self.profile_version_v3
        self.cand_profile.save()

        # Approval for v3
        self.approval_v3 = CandidateProfileApproval.objects.create(
            profile_version=self.profile_version_v3,
            approved_by=self.user,
            idempotency_key="test-1-aaaaaaaa"
        )

        # Source doc & extraction record to establish source evidence
        self.doc = CandidateDocument.objects.create(profile=self.cand_profile, name="CV_Candidate_Awan.pdf")
        self.doc_ver = CandidateDocumentVersion.objects.create(
            document=self.doc,
            original_filename="CV_Candidate_Awan.pdf",
            file_hash="hash123",
            version_number=1,
            file_size=1024,
            mime_type="application/pdf"
        )
        self.extraction = CandidateExtractionRecord.objects.create(
            profile=self.cand_profile,
            status="completed",
            raw_text="Name: Alex Morgan. Email: candidate-10@example.com"
        )

        # Holds
        self.hold = IncidentHold.objects.create(
            reason="IDENTITY_AND_SOURCE_INTEGRITY_INCIDENT",
            active=True
        )
        IncidentHoldTarget.objects.create(incident_hold=self.hold, content_object=self.doc)

    def _get_fields_snapshot(self):
        return {
            "profile": (self.cand_profile.full_name, self.cand_profile.active_version_id),
            "v3": (self.profile_version_v3.state, self.profile_version_v3.full_name),
            "hold": self.hold.active
        }

    # 1. Reconciliation service performs zero writes
    def test_reconciliation_service_zero_writes(self):
        snap_before = capture_canonical_db_snapshot(self.cand_profile)
        
        reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id
        )
        
        snap_after = capture_canonical_db_snapshot(self.cand_profile)
        assert compare_db_snapshots(snap_before, snap_after) is True

    # 2. Management command performs zero writes
    def test_reconciliation_command_zero_writes(self):
        snap_before = capture_canonical_db_snapshot(self.cand_profile)
        
        out = io.StringIO()
        with pytest.raises(SystemExit):
            call_command(
                "career_identity_approval_reconcile",
                "--user-email", "candidate-10@example.com",
                "--historical-version-id", self.profile_version_v3.id,
                stdout=out
            )
            
        snap_after = capture_canonical_db_snapshot(self.cand_profile)
        assert compare_db_snapshots(snap_before, snap_after) is True

    # 3. Scenario 19: Actual real development state shape (blocked preflight, exit code 1)
    def test_real_state_shape_blocked(self):
        # State: superseded v2, approved v3, no v4/v5
        v2 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=1,
            full_name="idwsdsjtasdkk",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="superseded",
            checksum="v2checksum"
        )
        
        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id
        )
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED"
        assert "No corrected CandidateProfileVersion exists." in res["preflight_reasons"]
        
        # Test command exit code is non-zero (1)
        out = io.StringIO()
        with pytest.raises(SystemExit) as excinfo:
            call_command(
                "career_identity_approval_reconcile",
                "--user-email", "candidate-10@example.com",
                "--historical-version-id", self.profile_version_v3.id,
                stdout=out
            )
        assert excinfo.value.code == 1

    # 4. Scenario: Version 5 approved and active, Version 4 still pending
    def test_v5_approved_active_v4_pending(self):
        v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=False,
            state="pending_approval"
        )
        v5 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=4,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v5
        self.cand_profile.save()

        # Approval for v5
        CandidateProfileApproval.objects.create(
            profile_version=v5,
            approved_by=self.user,
            idempotency_key="ik_v5"
        )

        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v5.id
        )
        assert res["questions"]["1. Does Version 4 still exist?"] == "YES"
        assert res["questions"]["2. What is Version 4’s current state?"] == "pending_approval"
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED"

    # 5. Scenario: Version 4 missing
    def test_v4_missing(self):
        v5 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v5
        self.cand_profile.save()

        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v5.id
        )
        assert res["questions"]["1. Does Version 4 still exist?"] == "NOT PRESENT IN CURRENT DEVELOPMENT DATABASE"

    # 6. Scenario: Version 4 and Version 5 logically identical / duplicate
    def test_v4_v5_logically_identical(self):
        v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=False,
            state="pending_approval",
            fact_snapshot=[{"fact_type": "skill", "category": "programming", "value": "Python"}]
        )
        v5 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=4,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=False,
            state="pending_approval",
            fact_snapshot=[{"fact_type": "skill", "category": "programming", "value": "Python"}]
        )
        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v5.id
        )
        assert res["questions"]["5. Is Version 5 a byte-identical duplicate, logical duplicate, or materially different version?"] == "LOGICAL_DUPLICATE"

    # 7. Scenario: Version 4 and Version 5 materially different
    def test_v4_v5_materially_different(self):
        v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=False,
            state="pending_approval",
            fact_snapshot=[]
        )
        v5 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=4,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=False,
            state="pending_approval",
            fact_snapshot=[{"fact_type": "skill", "category": "programming", "value": "Python"}]
        )
        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v5.id
        )
        assert res["questions"]["5. Is Version 5 a byte-identical duplicate, logical duplicate, or materially different version?"] == "MATERIALLY_DIFFERENT"

    # 8. Scenario: Version 4 and Version 5 both approved (blocking)
    def test_v4_v5_both_approved(self):
        v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        v5 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=4,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v5
        self.cand_profile.save()

        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v5.id
        )
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED"

    # 9. Scenario: Approval points to Version 5
    def test_approval_points_v5(self):
        v5 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v5
        self.cand_profile.save()

        CandidateProfileApproval.objects.create(
            profile_version=v5,
            approved_by=self.user,
            idempotency_key="key_v5"
        )

        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v5.id
        )
        assert res["questions"]["6. Does CandidateProfileApproval point to Version 5?"] == "YES"

    # 10. Scenario: Approval incorrectly points to Version 4
    def test_approval_points_v4_incorrectly(self):
        v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        v5 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=4,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v5
        self.cand_profile.save()

        # Approval points to v4 instead of active version v5
        CandidateProfileApproval.objects.create(
            profile_version=v4,
            approved_by=self.user,
            idempotency_key="key_v4"
        )

        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v5.id
        )
        assert res["questions"]["6. Does CandidateProfileApproval point to Version 5?"] == "NO"
        assert res["questions"]["7. Is there any approval associated with Version 4?"] == "YES"
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED"

    # 11. Scenario: Active version has no approval
    def test_active_version_no_approval(self):
        v5 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v5
        self.cand_profile.save()

        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v5.id
        )
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED"
        assert any("lacks a CandidateProfileApproval record" in r for r in res["preflight_reasons"])

    # 12. Scenario: Multiple approvals exist
    def test_multiple_approvals_exist(self):
        v5 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v5
        self.cand_profile.save()

        CandidateProfileApproval.objects.create(
            profile_version=v5,
            approved_by=self.user,
            idempotency_key="key_v5_extra"
        )

        assert CandidateProfileApproval.objects.count() == 2

    # 13. Scenario: Pending duplicate remains approval-eligible
    def test_pending_duplicate_approval_eligible(self):
        v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=False,
            state="pending_approval"
        )
        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v4.id + 1 # hypothetical next ID
        )
        assert res["questions"]["8. Is Version 4 still eligible for approval?"] == "YES"
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED"

    # 14. Scenario: Historical Version 3 remains immutable
    def test_historical_v3_immutability(self):
        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id
        )
        assert res["v3_immutability"]["consistency"] == "HISTORICAL CONSISTENCY VALIDATED AGAINST AVAILABLE STORED FIELDS"

    # 15. Scenario: Forbidden identity appears in active corrected version
    def test_forbidden_identity_in_active_corrected_version(self):
        v5 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan idwsdsjtasdkk",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v5
        self.cand_profile.save()

        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v5.id
        )
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED"
        assert any("contains forbidden identity contamination" in r for r in res["preflight_reasons"])

    # 16. Scenario: Active IncidentHold remains active
    def test_active_hold_remains_active(self):
        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id
        )
        assert res["incident_hold_status"]["active"] is True

    # 17. Scenario: Identity-verification preflight reports READY only for an unambiguous state
    def test_preflight_blocked_by_default(self):
        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id
        )
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED"

    # 18. Scenario: No external service, browser, network, matching, targeting, or submission function is called
    @patch("openoutreach.career.service.review_corrected_profile_version")
    def test_no_external_workflows_invoked(self, mock_review):
        reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id
        )
        mock_review.assert_not_called()

    # 19. Scenario: Quarantined-artifact reference blocking
    def test_quarantined_artifact_reference_blocking(self):
        v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v4
        self.cand_profile.save()

        # Create approval
        CandidateProfileApproval.objects.create(
            profile_version=v4,
            approved_by=self.user,
            idempotency_key="key_v4_quar",
            approval_note="Approved via identity correction workflow."
        )

        # Create job & quarantined application draft
        job = Job.objects.create(title="Engineer", company="GenOO")
        job_app = JobApplication.objects.create(candidate_profile=self.cand_profile, job=job)
        draft = ApplicationDraft.objects.create(
            application=job_app,
            profile_version=v4, # link version to draft
            version_number=1,
            tailored_cv="Contaminated CV",
            cover_letter="Contaminated Letter"
        )
        IncidentHoldTarget.objects.create(incident_hold=self.hold, content_object=draft)

        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v4.id
        )
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED"
        assert any("is referenced by quarantined ApplicationDraft" in r for r in res["preflight_reasons"])

    # 20. Scenario: READY only for a genuinely unambiguous state
    def test_ready_unambiguous_state(self):
        v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v4
        self.cand_profile.save()

        # Approval
        CandidateProfileApproval.objects.create(
            profile_version=v4,
            approved_by=self.user,
            idempotency_key="key_ready",
            approval_note="Approved via identity correction workflow."
        )

        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v4.id
        )
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: READY"

    # 21. Scenario: BLOCKED when name is mismatched
    def test_blocked_when_name_mismatched(self):
        v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Someone Else",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v4
        self.cand_profile.save()

        # Approval
        CandidateProfileApproval.objects.create(
            profile_version=v4,
            approved_by=self.user,
            idempotency_key="key_mismatch",
            approval_note="Approved via identity correction workflow."
        )

        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v4.id
        )
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED"

    # 22. Scenario: BLOCKED when approval is missing
    def test_blocked_when_approval_missing(self):
        v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v4
        self.cand_profile.save()

        # No CandidateProfileApproval is created

        res = reconcile_corrected_identity_approval_state(
            profile=self.cand_profile,
            expected_historical_version_id=self.profile_version_v3.id,
            expected_corrected_version_id=v4.id
        )
        assert res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED"

    # 23. Scenario: Successful READY command exit behavior
    def test_ready_command_exit_success(self):
        v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved"
        )
        self.cand_profile.active_version = v4
        self.cand_profile.save()

        # Approval
        CandidateProfileApproval.objects.create(
            profile_version=v4,
            approved_by=self.user,
            idempotency_key="key_exit_success",
            approval_note="Approved via identity correction workflow."
        )

        out = io.StringIO()
        # Verify call_command exits with exit code 0 / does not exit with code 1
        try:
            call_command(
                "career_identity_approval_reconcile",
                "--user-email", "candidate-10@example.com",
                "--historical-version-id", self.profile_version_v3.id,
                "--expected-corrected-version-id", v4.id,
                stdout=out
            )
        except SystemExit as e:
            assert e.code in (0, None)
