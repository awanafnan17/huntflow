"""
Production failure recovery and error classification service for OpenOutreach.
"""
import logging
from typing import Dict, Any, Tuple
from openoutreach.core.services.audit_service import record_audit_event

logger = logging.getLogger(__name__)

# Non-retriable error categories
NON_RETRIABLE_ERRORS = {
    "captcha_detected": "CAPTCHA / Security Challenge encountered",
    "platform_warning": "Platform account warning or restriction active",
    "identity_failure": "Identity verification or profile contamination failure",
    "suppressed_target": "Target lead is on workspace suppression record",
    "unauthorized_role": "User role does not possess permissions for action",
    "review_rejected": "Draft or application artifact was rejected in review",
    "entitlement_denied": "Workspace entitlement limit exceeded",
    "invalid_data": "Input data format or URL is invalid",
}

# Retriable error categories
RETRIABLE_ERRORS = {
    "network_timeout": "Transient network socket or HTTP timeout",
    "selector_mismatch": "DOM element selector not matched; page layout delay",
    "rate_limited_wait": "Rate limit pause recommended",
    "browser_crash": "Browser process terminated unexpectedly",
}


def classify_failure(error: Exception | str) -> Tuple[str, bool, str]:
    """
    Classifies an exception or error string into (category, retriable: bool, description).
    """
    err_str = str(error).lower()

    if "captcha" in err_str or "challenge" in err_str:
        return "captcha_detected", False, NON_RETRIABLE_ERRORS["captcha_detected"]
    elif "warning" in err_str or "restricted" in err_str or "checkpoint" in err_str:
        return "platform_warning", False, NON_RETRIABLE_ERRORS["platform_warning"]
    elif "identity" in err_str or "unverified" in err_str or "profile" in err_str:
        return "identity_failure", False, NON_RETRIABLE_ERRORS["identity_failure"]
    elif "suppress" in err_str or "opt_out" in err_str or "do_not_contact" in err_str:
        return "suppressed_target", False, NON_RETRIABLE_ERRORS["suppressed_target"]
    elif "unauthorized" in err_str or "forbidden" in err_str or "permission" in err_str:
        return "unauthorized_role", False, NON_RETRIABLE_ERRORS["unauthorized_role"]
    elif "rejected" in err_str:
        return "review_rejected", False, NON_RETRIABLE_ERRORS["review_rejected"]
    elif "entitlement" in err_str or "limit" in err_str and "exceeded" in err_str:
        return "entitlement_denied", False, NON_RETRIABLE_ERRORS["entitlement_denied"]
    elif "invalid" in err_str or "malformed" in err_str:
        return "invalid_data", False, NON_RETRIABLE_ERRORS["invalid_data"]
    elif "timeout" in err_str or "connection" in err_str or "socket" in err_str:
        return "network_timeout", True, RETRIABLE_ERRORS["network_timeout"]
    elif "selector" in err_str or "element" in err_str or "not found" in err_str:
        return "selector_mismatch", True, RETRIABLE_ERRORS["selector_mismatch"]
    elif "rate limit" in err_str:
        return "rate_limited_wait", True, RETRIABLE_ERRORS["rate_limited_wait"]
    elif "crash" in err_str or "closed" in err_str:
        return "browser_crash", True, RETRIABLE_ERRORS["browser_crash"]
    
    # Default to retriable unknown failure for safety
    return "unknown_transient_failure", True, f"Transient execution failure: {str(error)}"


def handle_service_failure(
    *,
    workspace,
    actor=None,
    target=None,
    action_key: str,
    error: Exception | str,
    attempt_count: int = 1,
    max_attempts: int = 3
) -> Dict[str, Any]:
    """
    Handles a service failure, classifies retriability, calculates backoff, and records audit event.
    """
    category, retriable, description = classify_failure(error)

    if attempt_count >= max_attempts:
        retriable = False
        description += f" (Max attempts {max_attempts} reached)"

    backoff_seconds = (2 ** attempt_count) * 60 if retriable else 0

    record_audit_event(
        workspace=workspace,
        event_type="execution_failed",
        actor=actor,
        target=target,
        result="failed" if not retriable else "retriable_failure",
        action_key=f"{action_key}_failure_attempt_{attempt_count}",
        metadata={
            "error_category": category,
            "retriable": retriable,
            "description": description,
            "attempt_count": attempt_count,
            "backoff_seconds": backoff_seconds,
            "error_message": str(error)
        }
    )

    return {
        "category": category,
        "retriable": retriable,
        "description": description,
        "attempt_count": attempt_count,
        "backoff_seconds": backoff_seconds,
        "error_message": str(error)
    }
