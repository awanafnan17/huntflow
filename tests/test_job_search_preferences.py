import pytest
from django.core.exceptions import ValidationError
from tests.factories import UserFactory
from openoutreach.core.models import Workspace, Campaign
from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, JobSearchPreference


@pytest.mark.django_db
def test_job_search_preference_creation_and_threshold_validation():
    """Verify JobSearchPreference creation, threshold validation, and range limits."""
    user = UserFactory(username="pref_user")
    ws = Workspace.objects.create(name="Pref WS", slug="pref-ws")
    camp = Campaign.objects.create(name="Job Camp", workspace=ws)
    cand_prof = CandidateProfile.objects.create(user=user, full_name="Candidate One", email="candidate-5@example.com")
    cand_ver = CandidateProfileVersion.objects.create(profile=cand_prof, version_number=1, full_name="Candidate One", email="candidate-5@example.com")

    pref = JobSearchPreference.objects.create(
        workspace=ws,
        campaign=camp,
        candidate_profile=cand_prof,
        candidate_profile_version=cand_ver,
        keywords=["Python", "Django"],
        job_titles=["Backend Engineer"],
        locations=["Remote"],
        match_threshold=75,
        execution_mode="discover_only",
    )

    assert pref.pk is not None
    assert pref.match_threshold == 75
    assert pref.execution_mode == "discover_only"

    # Invalid threshold < 0
    pref.match_threshold = -10
    with pytest.raises(ValidationError):
        pref.save()

    # Invalid threshold > 100
    pref.match_threshold = 105
    with pytest.raises(ValidationError):
        pref.save()


@pytest.mark.django_db
def test_job_search_preference_profile_version_mismatch():
    """Verify candidate_profile_version must belong to candidate_profile."""
    user1 = UserFactory(username="u1")
    user2 = UserFactory(username="u2")
    ws = Workspace.objects.create(name="WS Mismatch", slug="ws-mismatch")
    camp = Campaign.objects.create(name="Camp Mismatch", workspace=ws)

    p1 = CandidateProfile.objects.create(user=user1, full_name="P1", email="candidate-14@example.com")
    v1 = CandidateProfileVersion.objects.create(profile=p1, version_number=1, full_name="P1", email="candidate-14@example.com")

    p2 = CandidateProfile.objects.create(user=user2, full_name="P2", email="candidate-15@example.com")

    with pytest.raises(ValidationError):
        JobSearchPreference.objects.create(
            workspace=ws,
            campaign=camp,
            candidate_profile=p2,  # Mismatched profile
            candidate_profile_version=v1,
            match_threshold=80,
        )
