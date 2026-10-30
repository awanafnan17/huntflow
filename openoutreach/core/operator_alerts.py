# openoutreach/core/operator_alerts.py
from __future__ import annotations

import hashlib
import json
from django.utils import timezone
from openoutreach.core.models import OperatorAlert, OperatorAlertEvent
from openoutreach.core.operations import get_operations_snapshot, mask_sensitive_payload_recursive


def mask_alert_payload(value):
    """Recursively mask sensitive values in alert dictionary payload or metadata."""
    return mask_sensitive_payload_recursive(value)


def build_alert_fingerprint(alert: dict) -> str:
    """Generates a stable, deterministic fingerprint hash for an alert.
    
    Combines:
      - source
      - severity
      - title
      - fix_link
      - stable metadata attributes (e.g. task_id, campaign_id, launch_id, profile_id).
    Excludes volatile fields such as description timestamps, counts, or dates.
    """
    source = alert.get("source") or "operations"
    severity = alert.get("severity") or "info"
    title = alert.get("title") or ""
    fix_link = alert.get("fix_link") or ""

    # Pull stable attributes from metadata
    meta = alert.get("metadata") or {}
    stable_keys = ["task_id", "campaign_id", "launch_id", "profile_id", "daemon_status"]
    stable_meta = {}
    for sk in stable_keys:
        if sk in meta and meta[sk] is not None:
            stable_meta[sk] = meta[sk]

    # Deterministically stringify
    payload_dict = {
        "source": source,
        "severity": severity,
        "title": title,
        "fix_link": fix_link,
        "stable_meta": stable_meta,
    }
    
    # Recursively mask before hashing to ensure no raw credentials leak into the key digest
    payload_clean = mask_sensitive_payload_recursive(payload_dict)
    
    payload_str = json.dumps(payload_clean, sort_keys=True, default=str)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


def sync_operator_alerts(snapshot=None, operator=None, source="operations", dry_run=False) -> dict:
    """Synchronizes generated alerts from the operations snapshot into the OperatorAlert table.
    
    Only resolves/updates alerts belonging to the active sync source.
    Returns counts of mutations.
    """
    if snapshot is None:
        snapshot = get_operations_snapshot()

    now = timezone.now()
    created_count = 0
    updated_count = 0
    reopened_count = 0
    resolved_count = 0

    # Extract alerts from snapshot
    snap_alerts = snapshot.get("alerts") or []
    current_fingerprints = set()

    for alert_data in snap_alerts:
        # Guarantee correct default source
        if "source" not in alert_data:
            alert_data["source"] = source

        fingerprint = build_alert_fingerprint(alert_data)
        current_fingerprints.add(fingerprint)

        # Retrieve or create
        try:
            alert = OperatorAlert.objects.get(fingerprint=fingerprint)
            # Reopen resolved alerts if they reappear
            if alert.status == OperatorAlert.AlertStatus.RESOLVED:
                if not dry_run:
                    alert.status = OperatorAlert.AlertStatus.ACTIVE
                    alert.resolved_at = None
                    alert.last_triggered_at = now
                    alert.last_seen_at = now
                    alert.occurrence_count += 1
                    alert.save()
                    OperatorAlertEvent.objects.create(
                        alert=alert,
                        operator=operator,
                        event_type=OperatorAlertEvent.EventType.REOPENED,
                        note="Alert reappeared in operations snapshot; status reset to active."
                    )
                reopened_count += 1
            else:
                # Update details & stats
                if not dry_run:
                    alert.title = alert_data.get("title") or alert.title
                    alert.description = alert_data.get("description") or alert.description
                    alert.fix_link = alert_data.get("fix_link") or alert.fix_link
                    alert.metadata = mask_sensitive_payload_recursive(alert_data.get("metadata") or {})
                    alert.last_seen_at = now
                    alert.occurrence_count += 1
                    alert.save()
                updated_count += 1
        except OperatorAlert.DoesNotExist:
            if not dry_run:
                alert = OperatorAlert.objects.create(
                    fingerprint=fingerprint,
                    source=alert_data["source"],
                    severity=alert_data.get("severity") or "info",
                    status=OperatorAlert.AlertStatus.ACTIVE,
                    title=alert_data.get("title") or "",
                    description=alert_data.get("description") or "",
                    fix_link=alert_data.get("fix_link") or "",
                    first_seen_at=now,
                    last_seen_at=now,
                    last_triggered_at=now,
                    metadata=mask_sensitive_payload_recursive(alert_data.get("metadata") or {})
                )
                OperatorAlertEvent.objects.create(
                    alert=alert,
                    operator=operator,
                    event_type=OperatorAlertEvent.EventType.CREATED,
                    note="Alert logged for the first time."
                )
            created_count += 1

    # Transition alerts not present in this snapshot to RESOLVED
    # (Source-safe: only resolve alerts belonging to the same source scope)
    missing_alerts = OperatorAlert.objects.filter(
        source=source
    ).exclude(
        fingerprint__in=current_fingerprints
    ).exclude(
        status=OperatorAlert.AlertStatus.RESOLVED
    )

    for alert in missing_alerts:
        if not dry_run:
            alert.status = OperatorAlert.AlertStatus.RESOLVED
            alert.resolved_at = now
            alert.save()
            OperatorAlertEvent.objects.create(
                alert=alert,
                operator=operator,
                event_type=OperatorAlertEvent.EventType.RESOLVED,
                note="Alert is no longer active in snapshot; marked resolved by sync."
            )
        resolved_count += 1

    # Metrics count summaries
    total_active = OperatorAlert.objects.filter(status=OperatorAlert.AlertStatus.ACTIVE).count()
    total_acked = OperatorAlert.objects.filter(status=OperatorAlert.AlertStatus.ACKNOWLEDGED).count()
    total_snoozed = OperatorAlert.objects.filter(status=OperatorAlert.AlertStatus.SNOOZED).count()
    total_dismissed = OperatorAlert.objects.filter(status=OperatorAlert.AlertStatus.DISMISSED).count()

    return {
        "created_count": created_count,
        "updated_count": updated_count,
        "reopened_count": reopened_count,
        "resolved_count": resolved_count,
        "active_count": total_active,
        "acknowledged_count": total_acked,
        "snoozed_count": total_snoozed,
        "dismissed_count": total_dismissed,
    }


def get_operator_alert_summary() -> dict:
    """Safe, lightweight count queries for header badges and overview dashboards."""
    try:
        active = OperatorAlert.objects.filter(status=OperatorAlert.AlertStatus.ACTIVE).count()
        unacknowledged = OperatorAlert.objects.exclude(status=OperatorAlert.AlertStatus.ACKNOWLEDGED).count()
        snoozed = OperatorAlert.objects.filter(status=OperatorAlert.AlertStatus.SNOOZED).count()
        dismissed = OperatorAlert.objects.filter(status=OperatorAlert.AlertStatus.DISMISSED).count()
        resolved = OperatorAlert.objects.filter(status=OperatorAlert.AlertStatus.RESOLVED).count()
        return {
            "active": active,
            "unacknowledged": unacknowledged,
            "snoozed": snoozed,
            "dismissed": dismissed,
            "resolved": resolved,
        }
    except Exception:
        # Fail closed
        return {
            "active": 0,
            "unacknowledged": 0,
            "snoozed": 0,
            "dismissed": 0,
            "resolved": 0,
        }


def serialize_operator_alert(alert: OperatorAlert) -> dict:
    """Serializes OperatorAlert safely with recursive credential masking."""
    return {
        "id": alert.id,
        "fingerprint": alert.fingerprint,
        "source": alert.source,
        "severity": alert.severity,
        "status": alert.status,
        "title": alert.title,
        "description": alert.description,
        "fix_link": alert.fix_link,
        "first_seen_at": alert.first_seen_at.isoformat() if alert.first_seen_at else None,
        "last_seen_at": alert.last_seen_at.isoformat() if alert.last_seen_at else None,
        "last_triggered_at": alert.last_triggered_at.isoformat() if alert.last_triggered_at else None,
        "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
        "acknowledged_at": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
        "acknowledged_by": alert.acknowledged_by.username if alert.acknowledged_by else None,
        "snoozed_until": alert.snoozed_until.isoformat() if alert.snoozed_until else None,
        "dismissed_at": alert.dismissed_at.isoformat() if alert.dismissed_at else None,
        "dismissed_by": alert.dismissed_by.username if alert.dismissed_by else None,
        "occurrence_count": alert.occurrence_count,
        "metadata": mask_sensitive_payload_recursive(alert.metadata or {}),
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
        "updated_at": alert.updated_at.isoformat() if alert.updated_at else None,
    }
