"""
Synthetic High-Volume Execution Performance & Scaling Test Suite for HuntFlow.
Tests connection, application, and follow-up engines with synthetic data batches up to 1,000 targets.
"""
import pytest
import time
from datetime import timedelta
from django.utils import timezone
from openoutreach.core.models import Workspace, WorkspaceMembership, Campaign, CampaignPurpose, SenderIdentity
from openoutreach.crm.models.lead import Lead
from openoutreach.crm.models.deal import Deal
from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, CandidateFact, Job, JobApplication, ApplicationDraft, CandidateIdentityVerification
from openoutreach.crm.services.connection_engine import execute_connection_batch
from openoutreach.career.services.job_application_engine import execute_application_batch
from openoutreach.core.services.followup_engine import execute_followup_batch


@pytest.mark.django_db
class TestSyntheticHighVolumeScaling:

    @pytest.fixture(autouse=True)
    def setup_data(self, db, django_user_model):
        self.User = django_user_model
        self.user = self.User.objects.create_user(username="scale_owner", email="scale@example.com")
        self.ws = Workspace.objects.create(name="Scale Workspace", slug="scale-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")

        self.purpose = CampaignPurpose.objects.create(workspace=self.ws, name="Job Outreach", slug="job_outreach", is_system=True, is_active=True)
        self.sender = SenderIdentity.objects.create(workspace=self.ws, display_name="Scale Sender", email="scale@demo.invalid", verification_status="verified", is_active=True)
        self.campaign = Campaign.objects.create(workspace=self.ws, name="Scale Campaign", campaign_purpose=self.purpose, sender_identity=self.sender, is_active=True)

    def test_connection_batch_scaling(self):
        """Test connection batch execution across 1, 10, 100, 1000 synthetic targets."""
        for count in (1, 10, 100, 1000):
            leads = Lead.objects.bulk_create([
                Lead(
                    workspace=self.ws,
                    public_identifier=f"scale_lead_{count}_{i}",
                    first_name=f"Lead{i}",
                    last_name="Test",
                    linkedin_url=f"https://linkedin.com/in/scale-lead-{count}-{i}"
                )
                for i in range(count)
            ])

            t0 = time.time()
            res = execute_connection_batch(
                workspace=self.ws,
                campaign=self.campaign,
                leads=leads,
                batch_id=f"scale_conn_{count}"
            )
            t_elapsed = time.time() - t0

            assert res["processed"] == count
            assert res["successful"] == count
            assert t_elapsed < 30.0, f"Connection batch for {count} targets took {t_elapsed:.2f}s"

    def test_application_batch_scaling(self):
        """Test application batch execution across 1, 10, 100 synthetic jobs."""
        user2 = self.User.objects.create_user(username="cand_user_scale", email="cand2@example.com")
        candidate = CandidateProfile.objects.create(user=user2, full_name="Scale Candidate")
        CandidateIdentityVerification.objects.create(
            candidate_profile=candidate,
            status="verified",
            extracted_name="Scale Candidate",
            confirmed_name="Scale Candidate",
            verification_source="manual"
        )
        ver = CandidateProfileVersion.objects.create(profile=candidate, version_number=1, state="approved", full_name=candidate.full_name)
        candidate.active_version = ver
        candidate.save()
        CandidateFact.objects.create(profile_version=ver, fact_type="experience", category="work", value={"company": "Google", "title": "SWE"}, verified=True)

        for count in (1, 10, 100):
            jobs = Job.objects.bulk_create([
                Job(
                    company=f"Comp {i}",
                    title=f"Engineer {i}",
                    job_url=f"https://example.com/jobs/scale_{count}_{i}",
                    is_active=True
                )
                for i in range(count)
            ])

            t0 = time.time()
            res = execute_application_batch(
                workspace=self.ws,
                candidate=candidate,
                jobs=jobs,
                batch_id=f"scale_app_{count}"
            )
            t_elapsed = time.time() - t0

            assert res["processed"] == count, f"Processed error: {res}"
            assert res["successful"] == count, f"Successful error: {res}"
            assert t_elapsed < 120.0, f"Application batch for {count} jobs took {t_elapsed:.2f}s"

    def test_followup_batch_scaling(self):
        """Test follow-up batch execution across 1, 10, 100, 1000 synthetic deals."""
        for count in (1, 10, 100, 1000):
            leads = Lead.objects.bulk_create([
                Lead(
                    workspace=self.ws,
                    public_identifier=f"fol_lead_{count}_{i}",
                    linkedin_url=f"https://linkedin.com/in/fol-lead-{count}-{i}"
                )
                for i in range(count)
            ])
            deals = Deal.objects.bulk_create([
                Deal(
                    campaign=self.campaign,
                    lead=lead,
                    state="Qualified",
                    creation_date=timezone.now() - timedelta(days=5)
                )
                for lead in leads
            ])

            t0 = time.time()
            res = execute_followup_batch(
                workspace=self.ws,
                campaign=self.campaign,
                deals=deals,
                batch_id=f"scale_fol_{count}"
            )
            t_elapsed = time.time() - t0

            assert res["processed"] == count
            assert res["successful"] == count
            assert t_elapsed < 60.0, f"Followup batch for {count} deals took {t_elapsed:.2f}s"
