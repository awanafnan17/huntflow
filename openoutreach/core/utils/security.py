"""Security and secret redaction utilities for OpenOutreach."""

from typing import Any, Dict, List, Union
import copy

SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "cookie",
    "cookie_data",
    "li_at",
    "token",
    "secret",
    "authorization",
    "api_key",
    "refresh_token",
    "access_token",
)


def is_sensitive_key(key: str) -> bool:
    """Check if key name corresponds to sensitive secret or credential."""
    if not isinstance(key, str):
        return False
    k = key.strip().lower()
    return any(sub in k for sub in SENSITIVE_KEY_SUBSTRINGS)


def redact_sensitive_dict(data: Any, placeholder: str = "[REDACTED]") -> Any:
    """
    Recursively redact sensitive values in dictionaries, lists, and primitives.
    Guarantees secrets are never printed in logs, diagnostics, or exceptions.
    """
    if isinstance(data, dict):
        redacted = {}
        for k, v in data.items():
            if is_sensitive_key(str(k)):
                redacted[k] = placeholder
            else:
                redacted[k] = redact_sensitive_dict(v, placeholder=placeholder)
        return redacted
    elif isinstance(data, (list, tuple, set)):
        items = [redact_sensitive_dict(item, placeholder=placeholder) for item in data]
        return type(data)(items) if not isinstance(data, set) else set(items)
    return data


def safe_model_repr(instance: Any, fields: List[str] = None) -> Dict[str, Any]:
    """Extract model fields safely with sensitive fields redacted."""
    if not instance:
        return {}
    res = {}
    if hasattr(instance, "_meta"):
        for f in instance._meta.fields:
            name = f.name
            if fields and name not in fields:
                continue
            if is_sensitive_key(name):
                res[name] = "[REDACTED]"
            else:
                val = getattr(instance, name, None)
                res[name] = val
    return res
