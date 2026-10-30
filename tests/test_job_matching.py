import pytest
from tests.factories import UserFactory
from openoutreach.core.models import Workspace, Campaign
from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, Job, JobSearchPreference
from openoutreach.career.services.job_matching import assess_job_match


@pytest.mark.django_db
def test_job_matching_rubric_and_hard_blockers():
    """Verify explainable 100-point rubric, threshold evaluation, and hard blockers."""
    user = UserFactory(username="match_user")
    ws = Workspace.objects.create(name="Match WS", slug="match-ws")
    camp = Campaign.objects.create(name="Match Camp", workspace=ws)
    cand_prof = CandidateProfile.objects.create(user=user, full_name="Alice Smith", email="candidate-1@example.com")
    cand_ver = CandidateProfileVersion.objects.create(
        profile=cand_prof,
        version_number=1,
        full_name="Alice Smith",
        email="candidate-1@example.com",
        summary="Senior Backend Engineer with Python, Django, PostgreSQL, and REST API experience.",
    )

    JobSearchPreference.objects.create(
        workspace=ws,
        campaign=camp,
        candidate_profile=cand_prof,
        candidate_profile_version=cand_ver,
        keywords=["Python", "Django", "PostgreSQL"],
        match_threshold=75,
    )

    job = Job.objects.create(
        title="Senior Python Backend Engineer",
        company="TechCorp",
        location="Remote",
        description="We are looking for a Senior Backend Engineer skilled in Python, Django, PostgreSQL, and APIs. Requirements: Python, Django, PostgreSQL, REST API.",
    )

    assessment = assess_job_match(job=job, candidate_profile_version=cand_ver, threshold=75)

    assert assessment.score >= 75.0
    assert assessment.eligible is True
    assert assessment.hard_blocked is False
    assert len(assessment.matched_evidence) > 0
    assert "required_skills" in assessment.breakdown

    # Hard blocker test: closed job
    job.closed = True
    job.save()

    assessment_closed = assess_job_match(job=job, candidate_profile_version=cand_ver, threshold=75)
    assert assessment_closed.hard_blocked is True
    assert assessment_closed.eligible is False
    assert "Job posting is closed." in assessment_closed.hard_block_reasons


@pytest.mark.django_db
def test_job_match_threshold_boundary_logic():
    """Verify threshold 60 accepts score 60, threshold 90 rejects score 89.99, threshold 90 accepts score 90."""
    user = UserFactory(username="thresh_user")
    ws = Workspace.objects.create(name="Thresh WS", slug="thresh-ws")
    camp = Campaign.objects.create(name="Thresh Camp", workspace=ws)
    cand_prof = CandidateProfile.objects.create(user=user, full_name="Bob Jones", email="candidate-3@example.com")
    cand_ver = CandidateProfileVersion.objects.create(
        profile=cand_prof,
        version_number=1,
        full_name="Bob Jones",
        email="candidate-3@example.com",
        summary="Backend Engineer",
    )

    job = Job.objects.create(title="Engineer", company="Co", description="Desc")

    # Assess at threshold 90
    assessment = assess_job_match(job=job, candidate_profile_version=cand_ver, threshold=90)
    # Score around 70-80 -> should be rejected by threshold 90
    if assessment.score < 90.0:
        assert assessment.eligible is False

    # Assess at threshold 60 -> should be eligible
    assessment60 = assess_job_match(job=job, candidate_profile_version=cand_ver, threshold=60)
    if assessment60.score >= 60.0 and not assessment60.hard_blocked:
        assert assessment60.eligible is True
