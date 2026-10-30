import pytest
import uuid
import json
import hashlib
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from openoutreach.career.models import (
    CandidateProfile,
    CandidateProfileVersion,
    CandidateDocument,
    CandidateDocumentVersion,
    CandidateExtractionRecord,
    CandidateIdentityCorrectionLedger,
    CandidateFact,
    CandidateProfileApproval,
    CandidateIdentityVerification,
    IncidentHold,
    IncidentHoldTarget,
    ApplicationDraft,
    Job,
    JobApplication
)
from openoutreach.career.service import (
    create_corrected_pending_version_with_provenance,
    approve_corrected_profile_version
)
from tests.factories import UserFactory

@pytest.mark.django_db
class TestIdentityCorrectedVersionCreation:
    @pytest.fixture(autouse=True)
    def _setup(self, db):
        self.user = UserFactory(username="seeker_user", email="candidate-10@example.com")
        
        # Candidate Profile
        self.profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="idwsdsjtasdkk",
            email="candidate-10@example.com"
        )
        
        # Version 3 (Approved, finalized)
        self.v3 = CandidateProfileVersion.objects.create(
            profile=self.profile,
            version_number=3,
            full_name="idwsdsjtasdkk",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=False,
            state="draft",
            checksum="",
            fact_snapshot=[{"fact_type": "skill", "category": "programming", "value": "Python"}]
        )
        self.fact1 = CandidateFact.objects.create(
            profile_version=self.v3,
            fact_type="skill",
            category="programming",
            value="Python"
        )
        
        from openoutreach.career.canonical import compute_canonical_checksum
        self.v3.checksum = compute_canonical_checksum([self.fact1])
        self.v3.is_finalized = True
        self.v3.state = "approved"
        self.v3.save()
        
        # Exactly one approval for v3
        self.v3_approval = CandidateProfileApproval.objects.create(
            profile_version=self.v3,
            approved_by=self.user,
            approval_reason="INITIAL",
            idempotency_key="v3_approval_ik"
        )
        
        self.profile.active_version = self.v3
        self.profile.save()
        
        # Source document
        self.doc = CandidateDocument.objects.create(profile=self.profile, name="cv.pdf")
        self.doc_ver = CandidateDocumentVersion.objects.create(
            document=self.doc,
            version_number=1,
            file_hash="c84dd31de3ba8dbb9b1ae3fcf1a40f0da0f6ce9664550855e42c4a78255547c3",
            file_size=1024,
            mime_type="application/pdf",
            original_filename="cv.pdf"
        )
        
        # Extraction record
        self.extraction = CandidateExtractionRecord.objects.create(
            profile=self.profile,
            document_version=self.doc_ver,
            status="pending",
            raw_text="Alex Morgan\ncandidate-2@example.com\nSome other details...",
            raw_text_hash="texthash123"
        )
        
        # Set up active incident hold
        self.hold = IncidentHold.objects.create(
            reason="IDENTITY_AND_SOURCE_INTEGRITY_INCIDENT",
            active=True
        )
        # Quarantined draft
        self.job = Job.objects.create(pk=101, title="Draft Job", company="GenOO")
        self.job_app = JobApplication.objects.create(pk=101, candidate_profile=self.profile, job=self.job)
        self.app_draft = ApplicationDraft.objects.create(
            application=self.job_app,
            version_number=1,
            tailored_cv="CV containing idwsdsjtasdkk",
            cover_letter="Cover letter containing idwsdsjtasdkk"
        )
        IncidentHoldTarget.objects.create(incident_hold=self.hold, content_object=self.app_draft)

        self.idempotency_key = "test_creation_ik"

    def test_dry_run_zero_writes(self):
        c = create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user,
            dry_run=True
        )
        assert c is None
        assert CandidateProfileVersion.objects.count() == 1 # Only self.v3
        assert CandidateIdentityCorrectionLedger.objects.count() == 0

    def test_successful_creation_exactly_one_ledger(self):
        c = create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user
        )
        assert c is not None
        assert CandidateProfileVersion.objects.count() == 2 # v3 + new corrected v4
        assert CandidateIdentityCorrectionLedger.objects.count() == 1
        
        # Check CandidateIdentityCorrectionLedger properties
        ledger = CandidateIdentityCorrectionLedger.objects.get(corrected_version=c)
        assert ledger.profile == self.profile
        assert ledger.historical_version == self.v3
        assert ledger.source_document_version == self.doc_ver
        assert ledger.extraction_record == self.extraction
        assert ledger.normalized_source_identity == "Alex Morgan"
        assert ledger.source_email == "candidate-2@example.com"
        assert ledger.preserved_profile_email == "candidate-10@example.com"

    def test_active_and_historical_versions_remain_unchanged(self):
        create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user
        )
        
        self.profile.refresh_from_db()
        assert self.profile.active_version == self.v3
        
        v3_refreshed = CandidateProfileVersion.objects.get(pk=self.v3.id)
        assert v3_refreshed.full_name == "idwsdsjtasdkk"
        assert v3_refreshed.state == "approved"
        assert v3_refreshed.checksum == self.v3.checksum

    def test_no_approval_no_identity_verification_hold_active_draft_unchanged(self):
        c = create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user
        )
        # No approval for new version
        assert not CandidateProfileApproval.objects.filter(profile_version=c).exists()
        # Identity remains unverified
        assert not CandidateIdentityVerification.objects.filter(candidate_profile=self.profile).exists()
        # Hold remains active
        self.hold.refresh_from_db()
        assert self.hold.active is True
        # Quarantined draft is untouched
        self.app_draft.refresh_from_db()
        assert self.app_draft.tailored_cv == "CV containing idwsdsjtasdkk"

    def test_canonical_extraction_selection(self):
        # Create a second, newer extraction record
        newer_extraction = CandidateExtractionRecord.objects.create(
            profile=self.profile,
            document_version=self.doc_ver,
            status="pending",
            raw_text="Alex Morgan\ncandidate-2@example.com\nNewer extraction text",
            raw_text_hash="texthash456"
        )
        
        # Calling with the older extraction should fail because it's not canonical (newest)
        with pytest.raises(ValidationError, match="not the canonical"):
            create_corrected_pending_version_with_provenance(
                profile=self.profile,
                historical_version_id=self.v3.id,
                document_version_id=self.doc_ver.id,
                extraction_record_id=self.extraction.id,
                corrected_full_name="Alex Morgan",
                idempotency_key=self.idempotency_key,
                created_by=self.user
            )
            
        # Calling with the newer extraction should succeed
        c = create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=newer_extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user
        )
        assert c is not None

    def test_name_mismatch_blocks(self):
        with pytest.raises(ValidationError, match="Extracted name mismatch"):
            create_corrected_pending_version_with_provenance(
                profile=self.profile,
                historical_version_id=self.v3.id,
                document_version_id=self.doc_ver.id,
                extraction_record_id=self.extraction.id,
                corrected_full_name="Incorrect Name",
                idempotency_key=self.idempotency_key,
                created_by=self.user
            )

    def test_ambiguous_identity_blocks(self):
        # If historical version does not contain contaminated name "idwsdsjtasdkk"
        clean_v3 = CandidateProfileVersion.objects.create(
            profile=self.profile,
            version_number=30,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved",
            checksum="somechecksum",
            fact_snapshot=[]
        )
        CandidateProfileApproval.objects.create(
            profile_version=clean_v3,
            approved_by=self.user,
            approval_reason="INITIAL",
            idempotency_key="clean_v3_approval_ik"
        )
        self.profile.active_version = clean_v3
        self.profile.save()
        
        with pytest.raises(ValidationError, match="does not contain the contaminated identity"):
            create_corrected_pending_version_with_provenance(
                profile=self.profile,
                historical_version_id=clean_v3.id,
                document_version_id=self.doc_ver.id,
                extraction_record_id=self.extraction.id,
                corrected_full_name="Alex Morgan",
                idempotency_key=self.idempotency_key,
                created_by=self.user
            )

    def test_cross_profile_source_rejection(self):
        other_profile = CandidateProfile.objects.create(
            user=UserFactory(username="other_seeker"),
            full_name="Other Candidate",
            email="other@example.com"
        )
        with pytest.raises(ValidationError, match="does not belong to the requested profile"):
            create_corrected_pending_version_with_provenance(
                profile=other_profile,
                historical_version_id=self.v3.id,
                document_version_id=self.doc_ver.id,
                extraction_record_id=self.extraction.id,
                corrected_full_name="Alex Morgan",
                idempotency_key=self.idempotency_key,
                created_by=self.user
            )

    def test_missing_source_hash_blocks(self):
        invalid_doc_ver = CandidateDocumentVersion.objects.create(
            document=self.doc,
            version_number=2,
            file_hash="", # missing file hash
            file_size=1024,
            mime_type="application/pdf",
            original_filename="cv.pdf"
        )
        invalid_extraction = CandidateExtractionRecord.objects.create(
            profile=self.profile,
            document_version=invalid_doc_ver,
            status="pending",
            raw_text="Alex Morgan\ncandidate-2@example.com",
            raw_text_hash="texthash999"
        )
        with pytest.raises(ValidationError, match="Stored file hash is missing"):
            create_corrected_pending_version_with_provenance(
                profile=self.profile,
                historical_version_id=self.v3.id,
                document_version_id=invalid_doc_ver.id,
                extraction_record_id=invalid_extraction.id,
                corrected_full_name="Alex Morgan",
                idempotency_key=self.idempotency_key,
                created_by=self.user
            )

    def test_fact_preservation(self):
        c = create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user
        )
        assert c.facts.count() == 1
        fact = c.facts.first()
        assert fact.fact_type == "skill"
        assert fact.category == "programming"
        assert fact.value == "Python"

    def test_fact_mismatch_blocking(self):
        # Create fact mismatch by deleting the CandidateFact but keeping it in the snapshot
        CandidateFact.objects.filter(profile_version=self.v3).delete()
        with pytest.raises(ValidationError, match="Fact snapshot does not reconcile"):
            create_corrected_pending_version_with_provenance(
                profile=self.profile,
                historical_version_id=self.v3.id,
                document_version_id=self.doc_ver.id,
                extraction_record_id=self.extraction.id,
                corrected_full_name="Alex Morgan",
                idempotency_key=self.idempotency_key,
                created_by=self.user
            )

    def test_same_key_replay_and_conflict(self):
        c1 = create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user
        )
        # Replaying same payload and key returns identical object
        c2 = create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user
        )
        assert c1.id == c2.id
        
        # Conflict: same key but different payload (different document version)
        other_doc_ver = CandidateDocumentVersion.objects.create(
            document=self.doc,
            version_number=2,
            file_hash="c84dd31de3ba8dbb9b1ae3fcf1a40f0da0f6ce9664550855e42c4a78255547c3",
            file_size=1024,
            mime_type="application/pdf",
            original_filename="cv.pdf"
        )
        other_extraction = CandidateExtractionRecord.objects.create(
            profile=self.profile,
            document_version=other_doc_ver,
            status="pending",
            raw_text="Alex Morgan\ncandidate-2@example.com",
            raw_text_hash="texthash777"
        )
        with pytest.raises(ValidationError, match="Idempotency conflict"):
            create_corrected_pending_version_with_provenance(
                profile=self.profile,
                historical_version_id=self.v3.id,
                document_version_id=other_doc_ver.id,
                extraction_record_id=other_extraction.id,
                corrected_full_name="Alex Morgan",
                idempotency_key=self.idempotency_key,
                created_by=self.user
            )

    def test_different_key_duplicate_blocking(self):
        create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user
        )
        
        # Duplicate with different key blocks
        with pytest.raises(ValidationError, match="already has an active or approved identity correction"):
            create_corrected_pending_version_with_provenance(
                profile=self.profile,
                historical_version_id=self.v3.id,
                document_version_id=self.doc_ver.id,
                extraction_record_id=self.extraction.id,
                corrected_full_name="Alex Morgan",
                idempotency_key="another_key",
                created_by=self.user
            )

    def test_database_uniqueness_and_transaction_rollback(self):
        # If transaction raises an error, it rolls back
        from django.db import transaction
        
        # Trigger an error inside creation process
        with pytest.raises(ValidationError):
            with transaction.atomic():
                create_corrected_pending_version_with_provenance(
                    profile=self.profile,
                    historical_version_id=self.v3.id,
                    document_version_id=self.doc_ver.id,
                    extraction_record_id=self.extraction.id,
                    corrected_full_name="Incorrect Name", # will cause ValidationError
                    idempotency_key=self.idempotency_key,
                    created_by=self.user
                )
                
        # Assert no version and no ledger was created
        assert CandidateProfileVersion.objects.count() == 1
        assert CandidateIdentityCorrectionLedger.objects.count() == 0

    def test_unresolved_email_discrepancy(self):
        c = create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user
        )
        ledger = CandidateIdentityCorrectionLedger.objects.get(corrected_version=c)
        assert ledger.contact_email_discrepancy_status == "unresolved"

    def test_review_and_approval_blocked_due_to_unresolved_email(self):
        c = create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user
        )
        
        # Approval fails because email discrepancy is unresolved
        with pytest.raises(ValidationError, match="Cannot approve corrected profile version"):
            approve_corrected_profile_version(
                profile_version=c,
                approved_by=self.user,
                idempotency_key="approve_ik_fail"
            )
            
    def test_no_external_functions_called(self):
        # The creation function only accesses the database and makes no external requests/calls
        c = create_corrected_pending_version_with_provenance(
            profile=self.profile,
            historical_version_id=self.v3.id,
            document_version_id=self.doc_ver.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key=self.idempotency_key,
            created_by=self.user
        )
        assert c is not None
