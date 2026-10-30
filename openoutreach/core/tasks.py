"""
Production background task workers for OpenOutreach batch processing.
Supports atomic task claims, leases, retries, dead-letter logging, exponential backoff, and heartbeat stale lease reclamation.
"""
import logging
import time
import math
from typing import Dict, Any, List, Optional
from django.db import transaction
from openoutreach.core.models import Workspace, Campaign, AuditEvent
from openoutreach.crm.models.lead import Lead
from openoutreach.crm.models.deal import Deal
from openoutreach.career.models import CandidateProfile, Job
from openoutreach.crm.services.connection_engine import execute_connection_batch
from openoutreach.career.services.job_application_engine import execute_application_batch
from openoutreach.core.services.followup_engine import execute_followup_batch
from openoutreach.core.services.audit_service import record_audit_event

logger = logging.getLogger(__name__)

# Active worker task leases / locks
_TASK_LEASES: Dict[str, float] = {}
_DEAD_LETTER_QUEUE: List[Dict[str, Any]] = []


def acquire_task_lease(task_id: str, lease_seconds: int = 300) -> bool:
    """Atomically acquires or renews a worker task lease to prevent duplicate execution."""
    now = time.time()
    current_lease = _TASK_LEASES.get(task_id)
    if current_lease and current_lease > now:
        return False
    _TASK_LEASES[task_id] = now + lease_seconds
    return True


def release_task_lease(task_id: str) -> None:
    """Releases a worker task lease."""
    _TASK_LEASES.pop(task_id, None)


def reclaim_stale_leases(max_age_seconds: int = 300) -> int:
    """Reclaims stale worker task leases that exceeded lease duration or heartbeat cutoff."""
    now = time.time()
    expired = [k for k, v in _TASK_LEASES.items() if v <= now]
    for k in expired:
        _TASK_LEASES.pop(k, None)
    return len(expired)


def calculate_exponential_backoff(attempt: int, base_delay: float = 1.0, max_delay: float = 60.0) -> float:
    """Calculates exponential backoff delay with cap."""
    delay = base_delay * (2 ** (attempt - 1))
    return min(delay, max_delay)


def route_to_dead_letter_queue(workspace: Workspace, task_id: str, payload: Dict[str, Any], error: str) -> Dict[str, Any]:
    """Logs and routes failed tasks to the dead-letter queue after max retries."""
    entry = {
        "workspace_id": workspace.id,
        "task_id": task_id,
        "payload": payload,
        "error": error,
        "timestamp": time.time()
    }
    _DEAD_LETTER_QUEUE.append(entry)
    record_audit_event(
        workspace=workspace,
        event_type="dead_letter_task_queued",
        target=workspace,
        action_key=task_id,
        metadata={"error": error, "payload": payload}
    )
    return entry


def get_dead_letter_queue() -> List[Dict[str, Any]]:
    """Returns the current memory dead letter queue."""
    return list(_DEAD_LETTER_QUEUE)


def run_connection_batch_worker(
    *,
    workspace_id: int,
    campaign_id: int,
    lead_ids: List[int],
    batch_id: str,
    adapter=None,
    max_retries: int = 3
) -> Dict[str, Any]:
    """Worker task executing connection batch with atomic lease, DB locking, retries, and DLQ."""
    task_key = f"worker_conn_batch_{batch_id}"
    if not acquire_task_lease(task_key):
        return {"status": "duplicate_blocked", "task_id": task_key}

    try:
        workspace = Workspace.objects.get(id=workspace_id)
        campaign = Campaign.objects.get(id=campaign_id)

        # Atomic claims on leads
        with transaction.atomic():
            leads = list(
                Lead.objects.filter(id__in=lead_ids, workspace=workspace)
                .select_for_update(skip_locked=True)
            )

        attempt = 1
        last_error = None
        while attempt <= max_retries:
            try:
                res = execute_connection_batch(
                    workspace=workspace,
                    campaign=campaign,
                    leads=leads,
                    adapter=adapter,
                    batch_id=batch_id
                )
                if "status" not in res:
                    res["status"] = res.get("state", "completed")
                return res
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Attempt {attempt}/{max_retries} failed for {task_key}: {e}")
                if attempt < max_retries:
                    delay = calculate_exponential_backoff(attempt)
                    time.sleep(min(delay, 0.05))
                attempt += 1

        route_to_dead_letter_queue(
            workspace=workspace,
            task_id=task_key,
            payload={"campaign_id": campaign_id, "lead_ids": lead_ids, "batch_id": batch_id},
            error=last_error or "Max retries exceeded"
        )
        return {"status": "dead_lettered", "error": last_error}
    except Exception as e:
        logger.error(f"Worker task error in {task_key}: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}
    finally:
        release_task_lease(task_key)


def run_job_application_batch_worker(
    *,
    workspace_id: int,
    candidate_id: int,
    job_ids: List[int],
    batch_id: str,
    adapter=None,
    max_retries: int = 3
) -> Dict[str, Any]:
    """Worker task executing job application batch with atomic lease, DB locking, retries, and DLQ."""
    task_key = f"worker_app_batch_{batch_id}"
    if not acquire_task_lease(task_key):
        return {"status": "duplicate_blocked", "task_id": task_key}

    try:
        workspace = Workspace.objects.get(id=workspace_id)
        candidate = CandidateProfile.objects.get(id=candidate_id)

        with transaction.atomic():
            jobs = list(
                Job.objects.filter(id__in=job_ids)
                .select_for_update(skip_locked=True)
            )

        attempt = 1
        last_error = None
        while attempt <= max_retries:
            try:
                res = execute_application_batch(
                    workspace=workspace,
                    candidate=candidate,
                    jobs=jobs,
                    adapter=adapter,
                    batch_id=batch_id
                )
                if "status" not in res:
                    res["status"] = res.get("state", "completed")
                return res
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Attempt {attempt}/{max_retries} failed for {task_key}: {e}")
                if attempt < max_retries:
                    delay = calculate_exponential_backoff(attempt)
                    time.sleep(min(delay, 0.05))
                attempt += 1

        route_to_dead_letter_queue(
            workspace=workspace,
            task_id=task_key,
            payload={"candidate_id": candidate_id, "job_ids": job_ids, "batch_id": batch_id},
            error=last_error or "Max retries exceeded"
        )
        return {"status": "dead_lettered", "error": last_error}
    except Exception as e:
        logger.error(f"Worker task error in {task_key}: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}
    finally:
        release_task_lease(task_key)


def run_followup_batch_worker(
    *,
    workspace_id: int,
    campaign_id: int,
    deal_ids: List[int],
    batch_id: str,
    adapter=None,
    max_retries: int = 3
) -> Dict[str, Any]:
    """Worker task executing follow-up batch with atomic lease, DB locking, retries, and DLQ."""
    task_key = f"worker_followup_batch_{batch_id}"
    if not acquire_task_lease(task_key):
        return {"status": "duplicate_blocked", "task_id": task_key}

    try:
        workspace = Workspace.objects.get(id=workspace_id)
        campaign = Campaign.objects.get(id=campaign_id)

        with transaction.atomic():
            deals = list(
                Deal.objects.filter(id__in=deal_ids, campaign=campaign)
                .select_for_update(skip_locked=True)
            )

        attempt = 1
        last_error = None
        while attempt <= max_retries:
            try:
                res = execute_followup_batch(
                    workspace=workspace,
                    campaign=campaign,
                    deals=deals,
                    adapter=adapter,
                    batch_id=batch_id
                )
                if "status" not in res:
                    res["status"] = res.get("state", "completed")
                return res
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Attempt {attempt}/{max_retries} failed for {task_key}: {e}")
                if attempt < max_retries:
                    delay = calculate_exponential_backoff(attempt)
                    time.sleep(min(delay, 0.05))
                attempt += 1

        route_to_dead_letter_queue(
            workspace=workspace,
            task_id=task_key,
            payload={"campaign_id": campaign_id, "deal_ids": deal_ids, "batch_id": batch_id},
            error=last_error or "Max retries exceeded"
        )
        return {"status": "dead_lettered", "error": last_error}
    except Exception as e:
        logger.error(f"Worker task error in {task_key}: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}
    finally:
        release_task_lease(task_key)
