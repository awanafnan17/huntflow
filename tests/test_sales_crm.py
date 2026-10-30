import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from openoutreach.core.models import Campaign, Task
from openoutreach.crm.models import Lead, Deal, DealState, Outcome
from openoutreach.linkedin.models import LinkedInProfile, ActionLog


@pytest.mark.django_db
def test_sales_crm_authenticated_overview_rendering():
    """Verify authenticated access to Sales CRM overview tab."""
    user = User.objects.create_superuser(username="admin_crm")
    client = Client()
    client.force_login(user)

    resp = client.get("/dashboard/crm/", HTTP_HOST="127.0.0.1")
    assert resp.status_code == 200
    assert "Sales CRM Console" in resp.content.decode("utf-8")
    assert "Total Leads" in resp.content.decode("utf-8")


@pytest.mark.django_db
def test_sales_crm_tab_navigation():
    """Verify switching between Sales CRM tabs."""
    user = User.objects.create_superuser(username="admin_tabs")
    client = Client()
    client.force_login(user)

    for tab in ["overview", "leads", "deals", "pipeline", "activity"]:
        resp = client.get(f"/dashboard/crm/?tab={tab}", HTTP_HOST="127.0.0.1")
        assert resp.status_code == 200


@pytest.mark.django_db
def test_campaign_targeting_fields_persistence():
    """Verify detailed campaign targeting parameters save correctly."""
    user = User.objects.create(username="user_target")
    c = Campaign.objects.create(
        name="Targeting Campaign",
        is_active=True,
        target_job_titles="CEO, Founder",
        target_seniority="Executive",
        target_industries="B2B SaaS",
        target_locations="United States",
        target_company_size="11-50",
        target_keywords="AI outreach",
    )
    c.users.add(user)

    c.refresh_from_db()
    assert c.target_job_titles == "CEO, Founder"
    assert c.target_seniority == "Executive"
    assert c.target_industries == "B2B SaaS"
    assert c.target_locations == "United States"


@pytest.mark.django_db
def test_campaign_review_target_generation_post(mocker):
    """Verify target generation button POST action in campaign review."""
    mocker.patch(
        "openoutreach.linkedin.pipeline.search_keywords.generate_search_keywords",
        return_value=["CEO SaaS US", "Founder B2B Tech"]
    )
    mocker.patch(
        "openoutreach.linkedin.services.discovery_service.discover_campaign_targets",
        return_value={"status": "success", "keyword": "CEO SaaS US", "profiles_found": 2, "deals_created": 2, "strong_fits": 1}
    )
    user = User.objects.create_superuser(username="admin_gen")
    client = Client()
    client.force_login(user)

    c = Campaign.objects.create(name="Review Target Campaign", is_active=True)

    resp = client.post(
        f"/dashboard/campaigns/{c.pk}/review/",
        {"action": "generate_targets"},
        HTTP_HOST="127.0.0.1",
        follow=True
    )
    assert resp.status_code == 200
    assert "Discovered targets via &#x27;CEO SaaS US&#x27;" in resp.content.decode("utf-8") or "Discovered targets via" in resp.content.decode("utf-8")


@pytest.mark.django_db
def test_unified_task_dispatcher_job_application():
    """Verify daemon task dispatcher handles JOB_APPLICATION task type."""
    from openoutreach.core.daemon import handle_job_application_task

    user = User.objects.create(username="candidate_user")
    task = Task.objects.create(
        task_type=Task.TaskType.JOB_APPLICATION,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"candidate_id": 999, "job_id": 999, "draft_id": 999}
    )

    # Missing context should mark task failed gracefully without crashing
    handle_job_application_task(task, session=None, qualifiers=None)
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    assert task.payload.get("reason") == "missing_job_application_context"


@pytest.mark.django_db
def test_job_application_dispatcher_valid_execution(mocker):
    """Verify valid candidate/job/draft task reaches execute_job_application."""
    from openoutreach.core.daemon import handle_job_application_task
    from openoutreach.core.models import Workspace
    from openoutreach.career.models import CandidateProfile, Job, JobApplication, ApplicationDraft, CandidateProfileVersion

    ws = Workspace.objects.create(name="Default Workspace", slug="default-ws")
    user = User.objects.create(username="cand_user")
    cand = CandidateProfile.objects.create(user=user, full_name="Test Candidate")
    cand_ver = CandidateProfileVersion.objects.create(profile=cand, version_number=1, state="approved")
    cand.active_version = cand_ver
    cand.save()

    job = Job.objects.create(company="TechCorp", title="Lead Engineer", job_url="https://example.com/job/1")
    app = JobApplication.objects.create(candidate_profile=cand, job=job, status="approved")
    draft = ApplicationDraft.objects.create(application=app, version_number=1)

    mock_exec = mocker.patch(
        "openoutreach.career.services.job_application_engine.execute_job_application",
        return_value={"status": "submitted", "confirmation": "CONF-TEST-123"}
    )

    task = Task.objects.create(
        task_type=Task.TaskType.JOB_APPLICATION,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"candidate_id": cand.id, "job_id": job.id, "draft_id": draft.id}
    )

    handle_job_application_task(task, session=None, qualifiers=None)

    task.refresh_from_db()
    assert task.status == Task.Status.COMPLETED
    mock_exec.assert_called_once()


@pytest.mark.django_db
def test_job_application_dispatcher_incident_hold_blocked(mocker):
    """Verify IncidentHold blocks job application task execution."""
    from openoutreach.core.daemon import handle_job_application_task
    from openoutreach.core.models import Workspace
    from openoutreach.career.models import CandidateProfile, Job, JobApplication, ApplicationDraft, IncidentHold, IncidentHoldTarget

    ws = Workspace.objects.create(name="Hold WS", slug="hold-ws")
    user = User.objects.create(username="hold_user")
    cand = CandidateProfile.objects.create(user=user, full_name="Hold Candidate")
    job = Job.objects.create(company="HoldCorp", title="DevOps", job_url="https://example.com/job/2")
    app = JobApplication.objects.create(candidate_profile=cand, job=job, status="approved")
    draft = ApplicationDraft.objects.create(application=app, version_number=1)

    hold = IncidentHold.objects.create(reason="Safety Test Hold", active=True)
    IncidentHoldTarget.objects.create(incident_hold=hold, content_object=cand)

    task = Task.objects.create(
        task_type=Task.TaskType.JOB_APPLICATION,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"candidate_id": cand.id, "job_id": job.id, "draft_id": draft.id}
    )

    handle_job_application_task(task, session=None, qualifiers=None)
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    assert task.payload.get("reason") == "incident_hold_active"
