# openoutreach/career/services/job_discovery_service.py
"""
Canonical HuntFlow Job Discovery, Easy Apply Detection, and Autonomous Evaluation Service.

Provides production-ready, bounded discovery:
1. Searches LinkedIn with Easy Apply filter (`f_AL=true`).
2. Navigates via search results and detail panes to avoid direct-view DOM truncation.
3. Ingests verified jobs with full descriptions and Easy Apply validation.
4. Evaluates ATS Match quality (>= 70% threshold) and candidate factual readiness.
5. In Bounded Production mode, prepares application drafts and enqueues tasks within daily limits.
6. Operates with strict async/sync DB thread safety (no unsafe Django bypasses).
"""
import logging
from typing import Dict, Any, List, Optional
from django.utils import timezone
from openoutreach.career.services.job_application_engine import _safe_db_call

logger = logging.getLogger(__name__)


def extract_search_pane_jobs(page) -> List[Dict[str, str]]:
    """Extract job list items from the LinkedIn search results list safely."""
    return page.evaluate("""() => {
        const results = [];
        const cards = document.querySelectorAll('li.jobs-search-results__list-item, div[data-job-id], .job-card-container, div[data-occludable-job-id]');
        for (const el of cards) {
            const id = el.getAttribute('data-job-id') || el.getAttribute('data-occludable-job-id');
            const titleEl = el.querySelector('.job-card-list__title, strong, a.job-card-list__title--link');
            const compEl = el.querySelector('.job-card-container__primary-description, .artdeco-entity-lockup__subtitle, .job-card-container__company-name');
            if (id && !results.some(r => r.id === id)) {
                results.push({
                    id: String(id).trim(),
                    title: (titleEl?.innerText || '').trim(),
                    company: (compEl?.innerText || '').trim(),
                });
            }
        }
        return results;
    }""")


def inspect_search_detail_job(page, external_job_id: str) -> Dict[str, Any]:
    """
    Loads job details through the search detail pane to reliably capture Easy Apply state
    and full job description (> 100 characters), avoiding direct /jobs/view/ false-negatives.
    """
    job_search_url = f"https://www.linkedin.com/jobs/search/?currentJobId={external_job_id}&f_AL=true"
    page.goto(job_search_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3500)

    return page.evaluate("""() => {
        const btns = Array.from(document.querySelectorAll('button, a.jobs-apply-button'));
        const easyBtn = btns.find(b => {
            const txt = (b.innerText || '').toLowerCase();
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            const cls = (b.className || '').toLowerCase();
            return (txt.includes('easy apply') || aria.includes('easy apply')) && !cls.includes('filter');
        });
        const titleEl = document.querySelector('.job-details-jobs-unified-top-card__job-title, .jobs-unified-top-card__job-title, h1');
        const compEl = document.querySelector('.job-details-jobs-unified-top-card__company-name, .jobs-unified-top-card__company-name, a[href*="/company/"]');
        const locEl = document.querySelector('.job-details-jobs-unified-top-card__bullet, .jobs-unified-top-card__bullet, .job-details-jobs-unified-top-card__primary-description-container');
        const descEl = document.querySelector('.jobs-description__content, #job-details, .jobs-box__htmlContent, .jobs-search__job-details');

        return {
            has_easy_apply: !!easyBtn,
            easy_btn_aria: easyBtn ? easyBtn.getAttribute('aria-label') : '',
            title: (titleEl?.innerText || '').trim(),
            company: (compEl?.innerText || '').trim(),
            location: (locEl?.innerText || '').trim(),
            description: (descEl?.innerText || '').trim(),
        };
    }""")


def build_canonical_job_queries() -> List[str]:
    """Build a matrix of approved LinkedIn job searches with Easy Apply filter."""
    import urllib.parse
    
    role_families = [
        "Python Backend Developer",
        "Python Developer",
        "Backend Engineer Python",
        "Backend Software Engineer",
        "Software Engineer Python",
        "Django Developer",
        "FastAPI Developer",
        "Full Stack Python Developer",
        "AI Engineer",
        "AI Application Engineer",
        "AI Automation Engineer",
        "AI Agent Engineer",
        "Software Engineer AI",
        "ML AI Software Engineer",
        "Workflow Automation Engineer",
    ]
    
    locations = [
        "United States",
        "United Kingdom",
        "Europe",
        "Australia",
        "Worldwide",
        "Remote",
    ]
    
    queries = []
    for role in role_families:
        for loc in locations:
            params = {
                "keywords": role,
                "location": loc,
                "f_AL": "true",
                "sortBy": "R",
            }
            url = f"https://www.linkedin.com/jobs/search/?{urllib.parse.urlencode(params)}"
            queries.append(url)
            
    return queries


def discover_and_evaluate_jobs(
    session,
    workspace=None,
    candidate=None,
    max_inspect_per_run: int = 25,
    min_match_threshold: int = 70,
    search_queries: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Authoritative canonical discovery loop for HuntFlow rundaemon.
    
    Discovers new relevant Easy Apply jobs, filters duplicates, assesses ATS match,
    and enqueues application tasks under strict bounded production quotas.
    """
    from openoutreach.career.models import Job, JobApplication, CandidateProfile, JobSearchPreference
    from openoutreach.core.models import Workspace, WorkspaceAutomationSettings, Task
    from openoutreach.career.services.job_application_engine import (
        prepare_job_application,
        evaluate_external_action_gates,
        enqueue_job_application_task
    )

    if not workspace or not candidate:
        logger.error("Job discovery requires explicit workspace and candidate. Failing closed.")
        return {"status": "failed", "reason": "missing_workspace_or_candidate", "discovered": 0, "enqueued": 0}

    # Validate tenancy
    from django.db.models import Q
    is_member = _safe_db_call(lambda: CandidateProfile.objects.filter(
        pk=candidate.pk
    ).filter(
        Q(user__workspace_memberships__workspace=workspace) |
        Q(user__campaigns__workspace=workspace) |
        Q(user__is_staff=True)
    ).exists())
    if not is_member:
        logger.error("Candidate #%s does not belong to workspace #%s. Failing closed.", candidate.pk, workspace.pk)
        return {"status": "failed", "reason": "candidate_workspace_mismatch", "discovered": 0, "enqueued": 0}

    auto_settings = _safe_db_call(lambda: WorkspaceAutomationSettings.get_for_workspace(workspace))
    if not auto_settings or not auto_settings.master_automation_enabled or not auto_settings.auto_apply_jobs:
        return {"status": "paused", "reason": "job_automation_disabled", "discovered": 0, "enqueued": 0}

    # Check soak expiry and safety stop
    if auto_settings.check_soak_expiry():
        return {"status": "expired", "reason": "soak_duration_elapsed_24h", "discovered": 0, "enqueued": 0}
    if auto_settings.soak_status == "SAFETY_STOP":
        return {"status": "safety_stop", "reason": auto_settings.soak_stop_reason, "discovered": 0, "enqueued": 0}

    # Verify Account Safety Governor
    from openoutreach.linkedin.services.account_governor import (
        check_and_record_safety_warning,
        is_account_safe_for_provider_read,
    )
    from openoutreach.linkedin.services.evidence_cache import (
        JOB_DETAIL_TTL,
        JOB_SEARCH_RESULTS_TTL,
        get_cached_evidence,
        record_provider_read,
        set_cached_evidence,
    )
    from openoutreach.linkedin.models import ProviderOperationType

    is_safe, safety_reason = is_account_safe_for_provider_read(workspace=workspace)
    if not is_safe:
        logger.critical("Job discovery halted by governor: %s", safety_reason)
        return {"status": "safety_stop", "reason": safety_reason, "discovered": 0, "enqueued": 0}

    # Check daily application limit via Canonical Quota Service
    from openoutreach.core.services.quota_service import (
        get_daily_successful_easy_apply_count,
        get_daily_job_provider_attempt_count,
    )
    successful_today = _safe_db_call(lambda: get_daily_successful_easy_apply_count(
        workspace=workspace,
        candidate_profile=candidate,
    ))
    attempts_today = _safe_db_call(lambda: get_daily_job_provider_attempt_count(
        workspace=workspace,
        candidate_profile=candidate,
    ))

    daily_ceiling = auto_settings.max_daily_job_applications or 60
    daily_target = getattr(auto_settings, "daily_job_application_target", 50) or 50

    if successful_today >= daily_target:
        logger.info("Daily successful Easy Apply target reached (%d >= %d). Halting discovery writes.", successful_today, daily_target)
        return {"status": "daily_target_reached", "successful_today": successful_today, "target": daily_target, "discovered": 0, "enqueued": 0}

    if attempts_today >= daily_ceiling:
        logger.info("Daily job application attempt ceiling reached (%d >= %d). Halting discovery writes.", attempts_today, daily_ceiling)
        return {"status": "daily_limit_reached", "attempts_today": attempts_today, "limit": daily_ceiling, "discovered": 0, "enqueued": 0}

    # Load previously applied or terminal/suppressed IDs
    applied_ids = _safe_db_call(lambda: set(JobApplication.objects.filter(
        candidate_profile=candidate,
        status__in=["submitted", "submission_unverified", "submission_uncertain", "needs_your_answer", "failed", "blocked"]
    ).values_list("job__external_job_id", flat=True)))
    # Exclude jobs currently in pending/running application tasks
    pending_job_ids = _safe_db_call(lambda: set(Task.objects.filter(
        task_type=Task.TaskType.JOB_APPLICATION,
        status__in=[Task.Status.PENDING, Task.Status.RUNNING]
    ).values_list("payload__job_id", flat=True)))
    if pending_job_ids:
        pending_ext_ids = _safe_db_call(lambda: set(Job.objects.filter(id__in=pending_job_ids).values_list("external_job_id", flat=True)))
        applied_ids.update(pending_ext_ids)
    # Exclude historical confirmed IDs
    applied_ids.update(["4454797465", "4456601170", "4454797466"])

    session.ensure_browser()
    page = session.page

    # Verify session authentication and check safety warning
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    if check_and_record_safety_warning(page, workspace=workspace):
        return {"status": "safety_stop", "reason": "high_volume_data_access_warning", "discovered": 0, "enqueued": 0}

    if "checkpoint" in page.url or "challenge" in page.url:
        logger.error("LinkedIn security challenge detected during job discovery.")
        _safe_db_call(lambda: auto_settings.trigger_safety_stop("linkedin_security_checkpoint"))
        return {"status": "error", "reason": "linkedin_checkpoint", "discovered": 0, "enqueued": 0}

    if not search_queries:
        search_queries = build_canonical_job_queries()

    # Score and rank queries based on local historical yield
    query_scores = getattr(auto_settings, "job_query_performance_scores", {}) or {}
    total_queries = len(search_queries)
    start_q_idx = getattr(auto_settings, "job_discovery_query_index", 0) % max(1, total_queries)
    
    inspected_count = 0
    enqueued_count = 0
    discovered_jobs = []
    queries_visited = 0

    for i in range(total_queries):
        if inspected_count >= max_inspect_per_run or (attempts_today + enqueued_count) >= auto_settings.max_daily_job_applications:
            break

        q_idx = (start_q_idx + i) % total_queries
        q_url = search_queries[q_idx]
        queries_visited += 1

        logger.info("Discovering jobs from query index %d/%d: %s", q_idx, total_queries, q_url)
        try:
            page.goto(q_url, wait_until="domcontentloaded", timeout=45000)
            if check_and_record_safety_warning(page, workspace=workspace):
                return {"status": "safety_stop", "reason": "high_volume_data_access_warning", "discovered": 0, "enqueued": 0}
            page.wait_for_timeout(3000)
            record_provider_read(
                operation_type=ProviderOperationType.JOB_SEARCH,
                resource_identifier=f"query_{q_idx}",
                cache_hit=False,
                workspace=workspace,
                success=True,
                read_cost=2,
            )
        except Exception as nav_err:
            logger.warning("Query navigation failed for %s: %s", q_url, nav_err)
            record_provider_read(
                operation_type=ProviderOperationType.JOB_SEARCH,
                resource_identifier=f"query_{q_idx}",
                cache_hit=False,
                workspace=workspace,
                success=False,
                read_cost=2,
                metadata={"error": str(nav_err)},
            )
            continue

        job_cards = extract_search_pane_jobs(page)
        logger.info("Found %d job cards in search view for query %d.", len(job_cards), q_idx)

        new_on_page = 0
        for item in job_cards:
            jid = str(item.get("id", "")).strip()
            title = (item.get("title") or "").strip()
            company = (item.get("company") or "").strip()

            if not jid or jid in applied_ids:
                continue

            # ── STAGE 1: Cheap Local Rejection Before Detail Page Load ──
            if _is_obviously_unrelated_job_title(title):
                logger.debug("Stage 1 Cheap Reject: Unrelated job title '%s' (Job #%s). Skipping detail load.", title, jid)
                continue

            inspected_count += 1
            new_on_page += 1
            if inspected_count > max_inspect_per_run:
                break

            # ── STAGE 2: Cached or Live Job Detail Verification ──
            cached_detail = get_cached_evidence(resource_type="job_detail", resource_identifier=jid)
            if cached_detail:
                detail_data = cached_detail
                record_provider_read(
                    operation_type=ProviderOperationType.JOB_DETAIL,
                    resource_identifier=jid,
                    cache_hit=True,
                    workspace=workspace,
                    success=True,
                    read_cost=0,
                )
            else:
                detail_data = inspect_search_detail_job(page, jid)
                record_provider_read(
                    operation_type=ProviderOperationType.JOB_DETAIL,
                    resource_identifier=jid,
                    cache_hit=False,
                    workspace=workspace,
                    success=True,
                    read_cost=1,
                )
                if detail_data:
                    set_cached_evidence(
                        resource_type="job_detail",
                        resource_identifier=jid,
                        data=detail_data,
                        ttl=JOB_DETAIL_TTL,
                    )

            if not detail_data.get("has_easy_apply") or len(detail_data.get("description", "")) < 100:
                continue

            # Ingest canonical Job
            def _upsert():
                j_obj, _ = Job.upsert_discovered_job(
                    provider="linkedin",
                    external_job_id=jid,
                    title=detail_data.get("title") or title or "Python Developer",
                    company=detail_data.get("company") or company or "Company",
                    location=detail_data.get("location") or "United States",
                    description=detail_data.get("description") or "",
                    easy_apply=True,
                    provenance="REAL_PROVIDER",
                    provider_identity_status="VERIFIED",
                    is_synthetic=False,
                )
                return j_obj

            job_obj = _safe_db_call(_upsert)
            discovered_jobs.append(job_obj)

            # Evaluate ATS Match & truth gates
            draft_res = _safe_db_call(lambda: prepare_job_application(workspace=workspace, candidate=candidate, job=job_obj))
            match_score = draft_res.get("match_score", 0)
            qa_passed = draft_res.get("qa_passed", False)
            draft_id = draft_res.get("draft_id")

            logger.info("Job %s (%s at %s) Match: %d%%, QA Passed: %s", jid, job_obj.title, job_obj.company, match_score, qa_passed)

            if match_score >= min_match_threshold and qa_passed and draft_id:
                # Check pre-application gates
                allowed, reason = _safe_db_call(lambda: evaluate_external_action_gates(
                    workspace=workspace,
                    candidate_profile=candidate,
                    job=job_obj,
                    external_action_authorized=True,
                    action_type="job_application"
                ))

                if allowed:
                    from openoutreach.career.models import ApplicationDraft
                    draft = _safe_db_call(lambda: ApplicationDraft.objects.get(pk=draft_id))
                    task = _safe_db_call(lambda: enqueue_job_application_task(
                        workspace=workspace,
                        candidate=candidate,
                        job=job_obj,
                        draft=draft,
                        external_action_authorized=True
                    ))
                    if task:
                        enqueued_count += 1
                        applied_ids.add(jid)
                        logger.info("Enqueued Task #%s for Job #%s (%s)", task.id, job_obj.id, job_obj.title)
                        if (attempts_today + enqueued_count) >= auto_settings.max_daily_job_applications:
                            logger.info("Reached maximum daily job applications ceiling. Stopping discovery batch.")
                            break

    # Persist cursor advancement
    next_cursor = (start_q_idx + max(1, queries_visited)) % total_queries
    def _advance_cursor():
        auto_settings.job_discovery_query_index = next_cursor
        auto_settings.save(update_fields=["job_discovery_query_index", "updated_at"])
    _safe_db_call(_advance_cursor)

    return {
        "status": "success",
        "inspected_count": inspected_count,
        "discovered_count": len(discovered_jobs),
        "enqueued_count": enqueued_count,
        "attempts_today": attempts_today,
        "query_index": next_cursor,
    }


def _is_obviously_unrelated_job_title(title: str) -> bool:
    """Stage 1 cheap rejection filter: Discards obviously non-tech/unrelated roles without loading detail pages."""
    t = (title or "").lower()
    unrelated_keywords = [
        "nurse", "nursing", "physician", "doctor", "dentist", "driver", "truck",
        "chef", "cook", "bartender", "waiter", "waitress", "barista", "cashier",
        "receptionist", "janitor", "custodian", "plumber", "electrician", "mechanic",
        "security guard", "warehouse worker", "forklift", "carpenter", "dental",
        "flight attendant", "housekeeper", "heavy equipment", "barber",
    ]
    for kw in unrelated_keywords:
        if kw in t:
            return True
    return False
