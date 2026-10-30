# openoutreach/linkedin/browser_lease.py
"""Browser execution lease lock to prevent simultaneous Playwright browser conflicts."""
from __future__ import annotations

import logging
import time
from typing import Dict, Any, Optional

from django.utils import timezone

logger = logging.getLogger(__name__)

# Global in-memory lease state
_LEASE_STATE: Dict[str, Any] = {
    "is_locked": False,
    "owner": None,
    "acquired_at": None,
    "expires_at": None,
    "details": {},
}


def acquire_browser_lease(owner: str = "interactive_verification", ttl_seconds: int = 1800, details: Optional[Dict[str, Any]] = None) -> bool:
    """Acquire execution lease for exclusive browser profile access."""
    now = timezone.now()
    if _LEASE_STATE["is_locked"]:
        # Check if expired
        if _LEASE_STATE["expires_at"] and now > _LEASE_STATE["expires_at"]:
            logger.info("Previous browser lease held by %s expired. Reclaiming.", _LEASE_STATE["owner"])
            _LEASE_STATE["is_locked"] = False
        elif _LEASE_STATE["owner"] == owner:
            # Renew
            _LEASE_STATE["expires_at"] = timezone.now() + timezone.timedelta(seconds=ttl_seconds)
            return True
        else:
            logger.warning("Browser execution lease requested by %s but held by %s", owner, _LEASE_STATE["owner"])
            return False

    _LEASE_STATE["is_locked"] = True
    _LEASE_STATE["owner"] = owner
    _LEASE_STATE["acquired_at"] = now
    _LEASE_STATE["expires_at"] = now + timezone.timedelta(seconds=ttl_seconds)
    _LEASE_STATE["details"] = details or {}
    logger.info("Browser execution lease acquired by '%s' for %d seconds", owner, ttl_seconds)
    return True


def release_browser_lease(owner: str = "interactive_verification") -> bool:
    """Release execution lease, allowing daemon worker to resume browser automation."""
    if not _LEASE_STATE["is_locked"]:
        return True

    if _LEASE_STATE["owner"] == owner or owner == "force":
        logger.info("Browser execution lease released by '%s'", owner)
        _LEASE_STATE["is_locked"] = False
        _LEASE_STATE["owner"] = None
        _LEASE_STATE["acquired_at"] = None
        _LEASE_STATE["expires_at"] = None
        _LEASE_STATE["details"] = {}
        return True

    logger.warning("Attempted to release browser lease owned by '%s' with owner '%s'", _LEASE_STATE["owner"], owner)
    return False


def is_verification_browser_alive() -> bool:
    """Check if native verification browser process is alive."""
    from openoutreach.linkedin.vnc_service import _ACTIVE_NATIVE_SESSION
    pid = _ACTIVE_NATIVE_SESSION.get("pid")
    if pid:
        try:
            import os
            os.kill(pid, 0)
            return True
        except OSError:
            pass
    return False


def get_browser_lease_status() -> Dict[str, Any]:
    """Get current lease status dict, auto-extending if verification browser is alive."""
    now = timezone.now()
    if _LEASE_STATE["is_locked"]:
        if is_verification_browser_alive():
            # Heartbeat extension: Keep extending lease while process is running
            _LEASE_STATE["expires_at"] = now + timezone.timedelta(seconds=1800)
        elif _LEASE_STATE["expires_at"] and now > _LEASE_STATE["expires_at"]:
            _LEASE_STATE["is_locked"] = False
            _LEASE_STATE["owner"] = None

    return {
        "is_locked": _LEASE_STATE["is_locked"],
        "owner": _LEASE_STATE["owner"],
        "acquired_at": _LEASE_STATE["acquired_at"].isoformat() if _LEASE_STATE["acquired_at"] else None,
        "expires_at": _LEASE_STATE["expires_at"].isoformat() if _LEASE_STATE["expires_at"] else None,
        "details": _LEASE_STATE["details"],
    }
