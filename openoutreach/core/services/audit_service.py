# openoutreach/core/services/audit_service.py
"""Centralized Audit Event Logging Service for HuntFlow SaaS Platform."""
from __future__ import annotations

from typing import Any, Dict, Optional
from django.db import IntegrityError, transaction
from django.contrib.auth.models import User
from openoutreach.core.models import Workspace, AuditEvent

SENSITIVE_KEYS = {
    "token", "token_str", "token_hash", "password", "raw_password",
    "secret", "api_key", "llm_api_key", "bettercontact_api_key",
    "contacts_api_token", "session_id", "session_key", "cookie", "cookies",
    "credentials", "billing_secret", "stripe_signature"
}


def sanitize_metadata(data: Any) -> Any:
    """Recursively redact sensitive keys from audit event metadata."""
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if any(s in k.lower() for s in SENSITIVE_KEYS):
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = sanitize_metadata(v)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_metadata(item) for item in data]
    return data


def record_audit_event(
    *,
    workspace: Optional[Workspace] = None,
    event_type: str,
    actor: Optional[User] = None,
    target: Optional[Any] = None,
    result: str = "success",
    action_key: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[AuditEvent]:
    """
    Centralized Audit Logging Function.
    Ensures workspace isolation, standardized event types, sensitive value redaction,
    target model tracking, and idempotent event recording via action_key.
    """
    if workspace is None:
        return None

    target_type = ""
    target_id = ""
    if target is not None:
        target_type = target.__class__.__name__
        target_id = str(getattr(target, "pk", getattr(target, "id", "")))

    sanitized_payload = sanitize_metadata(metadata or {})

    # Check for existing idempotent audit event if action_key is provided
    if action_key:
        existing = AuditEvent.objects.filter(
            workspace=workspace,
            event_type=event_type,
            action_key=action_key,
        ).first()
        if existing:
            return existing

    try:
        from django.db import transaction
        with transaction.atomic():
            event = AuditEvent.objects.create(
                workspace=workspace,
                event_type=event_type,
                actor=actor if (actor and actor.is_authenticated) else None,
                target_type=target_type,
                target_id=target_id,
                result=result,
                action_key=action_key,
                payload=sanitized_payload,
            )
            return event
    except IntegrityError:
        # Fallback if concurrent transaction created the same action_key
        return AuditEvent.objects.filter(
            workspace=workspace,
            event_type=event_type,
            action_key=action_key,
        ).first()
    except Exception:
        return None
