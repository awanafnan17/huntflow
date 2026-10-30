import pytest
from tests.factories import UserFactory
from openoutreach.core.models import Workspace, Campaign
from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, JobSearchPreference
from openoutreach.career.services.linkedin_job_filters import build_linkedin_job_search_plan


@pytest.mark.django_db
def test_linkedin_job_filter_mapping_supported_and_unsupported():
    """Verify supported LinkedIn filters and unsupported local filters are separated correctly."""
    user = UserFactory(username="map_user")
    ws = Workspace.objects.create(name="Map WS", slug="map-ws")
    camp = Campaign.objects.create(name="Map Camp", workspace=ws)
    cand_prof = CandidateProfile.objects.create(user=user, full_name="Candidate Map", email="candidate-7@example.com")
    cand_ver = CandidateProfileVersion.objects.create(profile=cand_prof, version_number=1, full_name="Candidate Map", email="candidate-7@example.com")

    pref = JobSearchPreference.objects.create(
        workspace=ws,
        campaign=camp,
        candidate_profile=cand_prof,
        candidate_profile_version=cand_ver,
        keywords=["Python", "PostgreSQL"],
        job_titles=["Software Engineer"],
        locations=["Remote, US"],
        remote_options=["remote", "hybrid"],
        employment_types=["full_time"],
        experience_levels=["mid_senior"],
        date_posted="past_week",
        easy_apply_only=True,
        minimum_salary=120000,
        visa_sponsorship_required=True,
        excluded_companies=["Evil Corp"],
        match_threshold=80,
    )

    plan = build_linkedin_job_search_plan(pref)

    assert "linkedin_filters" in plan
    assert "local_filters" in plan
    assert "unsupported_filters" in plan

    li_f = plan["linkedin_filters"]
    assert li_f["keywords"] == "Python PostgreSQL"
    assert li_f["location"] == "Remote, US"
    assert li_f["easy_apply_only"] is True

    loc_f = plan["local_filters"]
    assert loc_f["match_threshold"] == 80
    assert loc_f["minimum_salary"] == 120000

    unsupp_names = [u["name"] for u in plan["unsupported_filters"]]
    assert "minimum_salary" in unsupp_names
    assert "visa_sponsorship_required" in unsupp_names
    assert "excluded_companies" in unsupp_names
