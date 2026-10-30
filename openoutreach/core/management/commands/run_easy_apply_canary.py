from django.core.management.base import BaseCommand
from django.utils import timezone
from openoutreach.career.models import CandidateProfile, Job, JobApplication, ApplicationDraft
from openoutreach.career.services.job_application_engine import prepare_job_application
from openoutreach.core.models import Workspace, Task
from openoutreach.core.daemon import handle_job_application_task
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.browser.registry import get_or_create_session


class Command(BaseCommand):
    help = "Run exactly one controlled real LinkedIn Easy Apply canary application via normal daemon dispatcher."

    def handle(self, *args, **options):
        from openoutreach.career.service import compute_job_match
        from openoutreach.linkedin.browser.easy_apply import extract_linkedin_job_id

        cp1 = CandidateProfile.objects.get(id=1)
        cpv = cp1.active_version
        ws = Workspace.objects.first()
        p3 = LinkedInProfile.objects.get(pk=3)
        session = get_or_create_session(p3)

        # Real LinkedIn Easy Apply job
        ext_id = "4440058610"
        job_url = f"https://www.linkedin.com/jobs/view/{ext_id}/"
        
        job, _ = Job.objects.get_or_create(
            job_url=job_url,
            defaults={
                "title": "Full Stack Developer",
                "company": "Ajaia | AI Consultancy",
                "location": "Remote",
                "description": "Full Stack Developer at Ajaia | AI Consultancy. Experience building AI-enabled product features or integrating large language models into applications. Familiarity with workflow automation, prompt design, full-stack APIs and Python backend engineering.",
                "source": "linkedin",
                "is_active": True
            }
        )

        # Dynamically compute factual match score
        match_res = compute_job_match(cpv, job)
        job.match_score = match_res.match_score
        job.match_explanation = match_res.match_explanation
        job.save(update_fields=["match_score", "match_explanation"])

        self.stdout.write(f"=== CANARY JOB: {job.title} at {job.company} (External ID: {ext_id}) ===")
        self.stdout.write(f"Factual Match Score: {job.match_score}%")

        # 1. Prepare draft
        prep = prepare_job_application(workspace=ws, candidate=cp1, job=job)
        self.stdout.write(f"Prepare Result: {prep}")

        draft = ApplicationDraft.objects.get(id=prep["draft_id"])
        draft.application.status = "approved"
        draft.application.save(update_fields=["status"])

        # 2. Create canonical PENDING Task
        task = Task.objects.create(
            task_type=Task.TaskType.JOB_APPLICATION,
            status=Task.Status.RUNNING,
            payload={
                "candidate_id": cp1.pk,
                "job_id": job.pk,
                "draft_id": draft.pk,
                "external_action_authorized": True
            },
            scheduled_at=timezone.now(),
        )
        self.stdout.write(f"Created Task #{task.pk}")

        # 3. Dispatch through production handler
        self.stdout.write("Invoking handle_job_application_task with live browser session...")
        handle_job_application_task(task=task, session=session, qualifiers={})

        # 4. Verify results
        task.refresh_from_db()
        app = JobApplication.objects.get(id=draft.application_id)
        self.stdout.write("\n=== DISPATCH & LIVE BROWSER EXECUTION RESULTS ===")
        self.stdout.write(f"Task #{task.pk} Status: {task.status}")
        self.stdout.write(f"Task Completed At: {task.completed_at}")
        self.stdout.write(f"Task Payload: {task.payload}")
        self.stdout.write(f"JobApplication #{app.id} Status: {app.status}")
        self.stdout.write(f"JobApplication Externally Confirmed: {app.externally_confirmed}")
        self.stdout.write(f"JobApplication Submission Date: {app.submission_date}")
