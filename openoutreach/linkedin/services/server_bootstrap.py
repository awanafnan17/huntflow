# openoutreach/linkedin/services/server_bootstrap.py
"""
HuntFlow Operator-Assisted Server Session Bootstrap Service.

Core Principles:
1. Server Session Ownership: Authentication sessions are created, owned, and extracted
   inside the HuntFlow server browser environment, NOT copied from an external Mac browser.
2. Storage State Trust Boundary: Does NOT accept client-posted storage state dictionaries.
   The context.storage_state() MUST be extracted directly from the HuntFlow-owned browser context.
3. No Credential Automation: Zero password storage, zero auto-typing, zero MFA solving,
   zero CAPTCHA/checkpoint bypass. Operator interacts manually when authorized.
4. Security & Isolation: Bounded strictly to exact workspace, profile, and operator.
   Protected by ProviderAccountLease throughout the entire lifecycle. Single-use token
   stored as SHA-256 hash. Replay, expiry, and cross-tenant access fail closed.
5. Invariants: Completing bootstrap transitions profile to 'needs_reauth' with
   session_origin='huntflow_managed'. It NEVER directly marks session_status='valid'.
   A separate canonical session verification is required to prove HEALTHY.
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import secrets
from datetime import timedelta
from typing import Any, Optional, Tuple

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from openoutreach.core.models import Workspace
from openoutreach.linkedin.exceptions import ServerBootstrapError
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderAccountLease,
    ServerBootstrapSession,
)
from openoutreach.linkedin.services.account_governor import (
    account_provider_lock,
    renew_provider_lease,
    get_active_profile_lease_token,
)

logger = logging.getLogger(__name__)


def hash_bootstrap_token(raw_token: str) -> str:
    """Return deterministic SHA-256 hex digest of a raw bootstrap token."""
    if not raw_token or not isinstance(raw_token, str):
        return ""
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def start_server_bootstrap(
    workspace: Workspace,
    linkedin_profile: LinkedInProfile,
    operator: User,
    ttl_seconds: int = 900,
) -> Tuple[Optional[ServerBootstrapSession], Optional[str], Optional[str]]:
    """Initiates an operator-assisted server session bootstrap for a provider profile.

    Returns: (session_instance, raw_single_use_token, error_message)
    Raw token is returned ONLY to caller for direct presentation; NEVER logged or saved plaintext.
    """
    # 1. Exact Tenant & Account Binding
    if not workspace or not linkedin_profile:
        return None, None, "missing_workspace_or_profile"

    if linkedin_profile.workspace_id != workspace.id:
        return None, None, "tenant_mismatch_profile_does_not_belong_to_workspace"

    # 2. Check single active bootstrap constraint
    now = timezone.now()
    active_bootstrap = ServerBootstrapSession.objects.filter(
        linkedin_profile=linkedin_profile,
        status__in=[ServerBootstrapSession.Status.PENDING, ServerBootstrapSession.Status.ACTIVE],
        expires_at__gt=now,
    ).first()
    if active_bootstrap:
        return None, None, "active_bootstrap_already_exists_for_profile"

    # Clean up any stale expired sessions
    ServerBootstrapSession.objects.filter(
        linkedin_profile=linkedin_profile,
        status__in=[ServerBootstrapSession.Status.PENDING, ServerBootstrapSession.Status.ACTIVE],
        expires_at__lte=now,
    ).update(status=ServerBootstrapSession.Status.EXPIRED)

    # 3. Concurrency Serialization: Acquire durable ProviderAccountLease
    # Aligned with bootstrap token TTL across processes and requests
    owner_token = f"tok_{secrets.token_hex(16)}"
    worker_id = f"bootstrap_{secrets.token_hex(6)}"
    expires_at = now + timedelta(seconds=ttl_seconds)

    with transaction.atomic():
        ProviderAccountLease.objects.filter(
            linkedin_profile=linkedin_profile,
            expires_at__lt=now,
        ).delete()

        existing_lease = ProviderAccountLease.objects.select_for_update().filter(
            linkedin_profile=linkedin_profile
        ).first()

        if existing_lease and existing_lease.expires_at >= now:
            return None, None, f"ProviderAccountLease conflict: Profile #{linkedin_profile.pk} is leased by another process"

        if existing_lease:
            existing_lease.locked_by = worker_id
            existing_lease.owner_token = owner_token
            existing_lease.acquired_at = now
            existing_lease.expires_at = expires_at
            existing_lease.active_operation = "server_bootstrap"
            existing_lease.workspace = workspace
            existing_lease.save(update_fields=["locked_by", "owner_token", "acquired_at", "expires_at", "active_operation", "workspace"])
        else:
            ProviderAccountLease.objects.create(
                linkedin_profile=linkedin_profile,
                workspace=workspace,
                locked_by=worker_id,
                owner_token=owner_token,
                acquired_at=now,
                expires_at=expires_at,
                active_operation="server_bootstrap",
            )

    lease_token = owner_token

    # 4. Generate Single-Use Cryptographic Token (Raw presented once, only hash stored)
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_bootstrap_token(raw_token)

    try:
        session = ServerBootstrapSession.objects.create(
            workspace=workspace,
            linkedin_profile=linkedin_profile,
            operator=operator,
            token_hash=token_hash,
            status=ServerBootstrapSession.Status.PENDING,
            lease_token=lease_token,
            expires_at=expires_at,
        )
    except Exception as exc:
        ProviderAccountLease.objects.filter(
            linkedin_profile=linkedin_profile,
            owner_token=owner_token,
        ).delete()
        return None, None, f"failed_to_create_bootstrap_session: {exc}"

    logger.info(
        "Initiated server bootstrap session #%d for profile #%d (tenant: %s, operator: %s, TTL: %ds)",
        session.pk,
        linkedin_profile.pk,
        getattr(workspace, "slug", workspace.pk),
        getattr(operator, "username", operator.pk),
        ttl_seconds,
    )
    return session, raw_token, None


def renew_server_bootstrap_lease(raw_token: str, extension_seconds: int = 300) -> Tuple[bool, str]:
    """Extends the ProviderAccountLease and token expiration for an active bootstrap session."""
    token_hash = hash_bootstrap_token(raw_token)
    if not token_hash:
        return False, "invalid_token"

    session = ServerBootstrapSession.objects.filter(token_hash=token_hash).first()
    if not session:
        return False, "bootstrap_session_not_found"

    if session.status not in [ServerBootstrapSession.Status.PENDING, ServerBootstrapSession.Status.ACTIVE]:
        return False, f"cannot_renew_in_status_{session.status}"

    if timezone.now() > session.expires_at:
        session.status = ServerBootstrapSession.Status.EXPIRED
        session.save(update_fields=["status"])
        return False, "bootstrap_session_expired"

    # Extend lease in DB
    extended = renew_provider_lease(
        linkedin_profile=session.linkedin_profile,
        owner_token=session.lease_token,
        extension_seconds=extension_seconds,
    )
    session.expires_at = timezone.now() + timedelta(seconds=extension_seconds)
    session.save(update_fields=["expires_at"])
    return True, "ok"


def complete_server_bootstrap(
    raw_token: str,
    server_browser_context_or_session: Any,
) -> Tuple[bool, str]:
    """Completes a server bootstrap session by capturing server storage state.

    CRITICAL SECURITY & TRUST BOUNDARY:
    - Does NOT accept raw client storage state dicts.
    - server_browser_context_or_session MUST be a trusted server-side Playwright
      BrowserContext or AccountSession instance that owns .storage_state().
    - Invariants enforced:
      1. Single-use: Session must be PENDING or ACTIVE, not previously COMPLETED.
      2. Storage state extracted internally inside HuntFlow server process.
      3. Validated: non-empty cookies list containing li_at.
      4. Stamped: session_origin = 'huntflow_managed'.
      5. Safe: session_status = 'needs_reauth' (never directly marks 'valid').
      6. Teardown: Browser context closed and ProviderAccountLease released.
    """
    token_hash = hash_bootstrap_token(raw_token)
    if not token_hash:
        return False, "invalid_token"

    with transaction.atomic():
        session = ServerBootstrapSession.objects.select_for_update().filter(token_hash=token_hash).first()
        if not session:
            return False, "bootstrap_session_not_found"

        if session.status == ServerBootstrapSession.Status.COMPLETED:
            return False, "token_already_consumed_replay_rejected"

        if session.status in [ServerBootstrapSession.Status.ABORTED, ServerBootstrapSession.Status.FAILED, ServerBootstrapSession.Status.EXPIRED]:
            return False, f"bootstrap_session_inactive_{session.status}"

        if timezone.now() > session.expires_at:
            session.status = ServerBootstrapSession.Status.EXPIRED
            session.save(update_fields=["status"])
            _release_bootstrap_lease(session)
            return False, "bootstrap_session_expired"

        # ── TRUST BOUNDARY ENFORCEMENT ──
        # Reject raw dictionaries passed directly from clients
        if isinstance(server_browser_context_or_session, dict):
            session.status = ServerBootstrapSession.Status.FAILED
            session.failure_reason = "client_raw_storage_state_rejected_must_use_server_browser_context"
            session.save(update_fields=["status", "failure_reason"])
            _release_bootstrap_lease(session)
            return False, "client_storage_state_rejected_must_use_server_browser_context"

        # Must have storage_state callable method on server object
        storage_state_func = None
        if hasattr(server_browser_context_or_session, "storage_state") and callable(server_browser_context_or_session.storage_state):
            storage_state_func = server_browser_context_or_session.storage_state
        elif hasattr(server_browser_context_or_session, "context") and hasattr(server_browser_context_or_session.context, "storage_state"):
            storage_state_func = server_browser_context_or_session.context.storage_state

        if not storage_state_func:
            session.status = ServerBootstrapSession.Status.FAILED
            session.failure_reason = "server_browser_context_missing_storage_state_method"
            session.save(update_fields=["status", "failure_reason"])
            _release_bootstrap_lease(session)
            return False, "server_browser_context_missing_storage_state_method"

        try:
            storage_state = storage_state_func()
        except Exception as exc:
            session.status = ServerBootstrapSession.Status.FAILED
            session.failure_reason = f"failed_to_extract_server_storage_state: {exc}"
            session.save(update_fields=["status", "failure_reason"])
            _release_bootstrap_lease(session)
            return False, f"failed_to_extract_server_storage_state: {exc}"

        # ── VALIDATE AND PERSIST TRUSTED SERVER STATE ──
        try:
            profile = session.linkedin_profile
            profile.persist_managed_storage_state(storage_state, caller_lease_token=session.lease_token)
            profile.bootstrap_completed_at = timezone.now()
            profile.session_status = "needs_reauth"
            profile.session_error_details = "HuntFlow-managed server session bootstrap completed. Canonical session verification required."
            profile.save(update_fields=["session_origin", "bootstrap_completed_at", "session_status", "session_error_details"])
        except Exception as exc:
            session.status = ServerBootstrapSession.Status.FAILED
            session.failure_reason = f"persistence_failure: {exc}"
            session.save(update_fields=["status", "failure_reason"])
            _release_bootstrap_lease(session)
            return False, f"persistence_failure: {exc}"

        # Mark bootstrap COMPLETED (single-use consumed)
        session.status = ServerBootstrapSession.Status.COMPLETED
        session.completed_at = timezone.now()
        session.save(update_fields=["status", "completed_at"])

    # Teardown: close server browser if closable and release lease
    try:
        if hasattr(server_browser_context_or_session, "close") and callable(server_browser_context_or_session.close):
            server_browser_context_or_session.close()
    except Exception as e:
        logger.debug("Browser close exception during bootstrap completion: %s", e)

    _release_bootstrap_lease(session)
    logger.info("Server bootstrap successfully completed for profile #%d (origin: huntflow_managed)", profile.pk)
    return True, "bootstrap_completed_successfully"


def abort_server_bootstrap(
    raw_token: str,
    reason: str = "operator_aborted",
    server_browser_context_or_session: Any = None,
) -> Tuple[bool, str]:
    """Aborts an in-progress server bootstrap session, closes browser, and releases lease."""
    token_hash = hash_bootstrap_token(raw_token)
    if not token_hash:
        return False, "invalid_token"

    session = ServerBootstrapSession.objects.filter(token_hash=token_hash).first()
    if not session:
        return False, "bootstrap_session_not_found"

    if session.status in [ServerBootstrapSession.Status.COMPLETED, ServerBootstrapSession.Status.ABORTED]:
        return False, f"session_already_{session.status}"

    session.status = ServerBootstrapSession.Status.ABORTED
    session.aborted_at = timezone.now()
    session.failure_reason = reason
    session.save(update_fields=["status", "aborted_at", "failure_reason"])

    if server_browser_context_or_session and hasattr(server_browser_context_or_session, "close"):
        try:
            server_browser_context_or_session.close()
        except Exception:
            pass

    _release_bootstrap_lease(session)
    logger.info("Aborted server bootstrap session #%d (reason: %s)", session.pk, reason)
    return True, "bootstrap_aborted"


def _release_bootstrap_lease(session: ServerBootstrapSession) -> None:
    """Safely releases the ProviderAccountLease bound to a bootstrap session."""
    try:
        from openoutreach.linkedin.models import ProviderAccountLease
        if session.lease_token:
            ProviderAccountLease.objects.filter(
                linkedin_profile_id=session.linkedin_profile_id,
                owner_token=session.lease_token,
            ).delete()
        else:
            ProviderAccountLease.objects.filter(
                linkedin_profile_id=session.linkedin_profile_id,
                active_operation="server_bootstrap",
            ).delete()
    except Exception as e:
        logger.warning("Error releasing bootstrap lease for session #%d: %s", session.pk, e)


def cleanup_abandoned_bootstrap_sessions(workspace: Optional[Workspace] = None) -> int:
    """Periodic or startup cleanup for expired/abandoned bootstrap sessions and leases."""
    now = timezone.now()
    qs = ServerBootstrapSession.objects.filter(
        status__in=[ServerBootstrapSession.Status.PENDING, ServerBootstrapSession.Status.ACTIVE],
        expires_at__lte=now,
    )
    if workspace:
        qs = qs.filter(workspace=workspace)

    cleaned = 0
    for sess in qs:
        sess.status = ServerBootstrapSession.Status.EXPIRED
        sess.failure_reason = "expired_before_completion"
        sess.viewer_connected = False
        sess.viewer_connection_id = ""
        sess.save(update_fields=["status", "failure_reason", "viewer_connected", "viewer_connection_id"])
        _release_bootstrap_lease(sess)
        cleaned += 1
    return cleaned


def claim_bootstrap_viewer(session_id: int, connection_id: str, token_hash: str) -> bool:
    """Atomically claims viewer ownership for a bootstrap session in the database."""
    now = timezone.now()
    rows = ServerBootstrapSession.objects.filter(
        id=session_id,
        viewer_connected=False,
        token_hash=token_hash,
        status__in=[ServerBootstrapSession.Status.PENDING, ServerBootstrapSession.Status.ACTIVE],
        expires_at__gt=now,
    ).update(
        viewer_connected=True,
        viewer_attached_at=now,
        viewer_connection_id=connection_id,
        status=ServerBootstrapSession.Status.ACTIVE,
        last_heartbeat_at=now,
    )
    return rows > 0


def release_bootstrap_viewer(session_id: int, connection_id: str) -> bool:
    """Releases viewer ownership if bound to the specified connection_id."""
    rows = ServerBootstrapSession.objects.filter(
        id=session_id,
        viewer_connection_id=connection_id,
    ).update(
        viewer_connected=False,
        viewer_connection_id="",
    )
    return rows > 0


async def complete_server_bootstrap_from_runtime(
    bootstrap_session_id: int,
    browser_context: Any,
    caller_lease_token: str = "",
) -> Tuple[bool, str]:
    """Internal trusted completion service called directly by the server bootstrap runtime.

    TRUST BOUNDARY:
    - Never callable with client-submitted storage state dicts.
    - browser_context MUST be an active server-side Playwright BrowserContext.
    - Extracts context.storage_state() internally (awaiting if coroutine).
    - Validates presence of authentication cookies (li_at or synthetic_auth).
    - Persists managed state encrypted, stamps session_origin='huntflow_managed',
      sets session_status='needs_reauth', and closes the browser context.
    """
    if isinstance(browser_context, dict):
        raise ServerBootstrapError("client_storage_state_rejected_must_use_server_browser_context")

    if not browser_context or not hasattr(browser_context, "storage_state"):
        raise ServerBootstrapError("browser_context_missing_storage_state_method")

    # Capture storage state from the server browser context
    try:
        raw_state = browser_context.storage_state()
        if inspect.iscoroutine(raw_state):
            storage_state = await raw_state
        else:
            storage_state = raw_state
    except Exception as exc:
        def _fail_session():
            sess = ServerBootstrapSession.objects.filter(id=bootstrap_session_id).first()
            if sess:
                sess.status = ServerBootstrapSession.Status.FAILED
                sess.failure_reason = f"failed_to_extract_server_storage_state: {exc}"
                sess.save(update_fields=["status", "failure_reason"])
                _release_bootstrap_lease(sess)
        await sync_to_async(_fail_session)()
        return False, f"failed_to_extract_server_storage_state: {exc}"

    # Validate storage state structure and auth cookie
    cookies = storage_state.get("cookies", []) if isinstance(storage_state, dict) else []
    auth_cookie = any(
        isinstance(c, dict) and c.get("name") in ["li_at", "synthetic_auth"] and bool(c.get("value"))
        for c in cookies
    )
    if not auth_cookie:
        return False, "missing_auth_cookie_in_server_storage_state"

    def _persist():
        with transaction.atomic():
            session = ServerBootstrapSession.objects.select_for_update().filter(id=bootstrap_session_id).first()
            if not session:
                return False, "bootstrap_session_not_found"

            if session.status == ServerBootstrapSession.Status.COMPLETED:
                return False, "token_already_consumed_replay_rejected"

            if session.status in [ServerBootstrapSession.Status.ABORTED, ServerBootstrapSession.Status.FAILED, ServerBootstrapSession.Status.EXPIRED]:
                return False, f"bootstrap_session_inactive_{session.status}"

            if timezone.now() > session.expires_at:
                session.status = ServerBootstrapSession.Status.EXPIRED
                session.save(update_fields=["status"])
                _release_bootstrap_lease(session)
                return False, "bootstrap_session_expired"

            if caller_lease_token and session.lease_token and caller_lease_token != session.lease_token:
                return False, "caller_lease_token_mismatch"

            # Persist managed state
            try:
                profile = session.linkedin_profile
                profile.persist_managed_storage_state(storage_state, caller_lease_token=session.lease_token)
                profile.bootstrap_completed_at = timezone.now()
                profile.session_status = "needs_reauth"
                profile.session_error_details = "HuntFlow-managed server session bootstrap completed. Canonical session verification required."
                profile.save(update_fields=["session_origin", "bootstrap_completed_at", "session_status", "session_error_details"])
            except Exception as exc:
                session.status = ServerBootstrapSession.Status.FAILED
                session.failure_reason = f"persistence_failure: {exc}"
                session.save(update_fields=["status", "failure_reason"])
                _release_bootstrap_lease(session)
                return False, f"persistence_failure: {exc}"

            session.status = ServerBootstrapSession.Status.COMPLETED
            session.completed_at = timezone.now()
            session.viewer_connected = False
            session.viewer_connection_id = ""
            session.save(update_fields=["status", "completed_at", "viewer_connected", "viewer_connection_id"])
            _release_bootstrap_lease(session)
            logger.info("Server bootstrap successfully completed from runtime for profile #%d (origin: huntflow_managed)", profile.pk)
            return True, "bootstrap_completed_successfully"

    ok, msg = await sync_to_async(_persist)()

    # Teardown server browser context
    try:
        if hasattr(browser_context, "close") and callable(browser_context.close):
            close_res = browser_context.close()
            if inspect.iscoroutine(close_res):
                await close_res
    except Exception as e:
        logger.debug("Browser context close exception during bootstrap completion: %s", e)

    return ok, msg

