# openoutreach/core/management/commands/test_llm_config.py
"""Test LLM Configuration management command (Batch 5.6).

Loads saved SiteConfig credentials and runs verify_llm_credentials to ping
the configured provider. Outputs results and sanitizes error messages.
"""
from __future__ import annotations

import re
import threading
from django.core.management.base import BaseCommand


def sanitize_and_classify_llm_error(error_str: str, api_key: str) -> tuple[str, str]:
    """Sanitize the error string to remove any API keys and classify into a category."""
    if not error_str:
        return "Unknown provider error", "Empty error message returned."

    sanitized = error_str
    if api_key:
        escaped_key = re.escape(api_key)
        sanitized = re.sub(escaped_key, "********", sanitized)
    sanitized = re.sub(r"sk-[a-zA-Z0-9]{20,}", "********", sanitized)
    sanitized = re.sub(r"AIzaSy[a-zA-Z0-9_-]{20,}", "********", sanitized)

    err_lower = sanitized.lower()

    if any(x in err_lower for x in ["api key", "api_key", "unauthorized", "invalid_api_key", "auth", "api-key", "401"]):
        category = "Authentication/API key error"
    elif any(x in err_lower for x in ["api base", "baseurl", "host", "connection", "timeout", "unreachable", "resolve", "http"]):
        category = "Base URL/network/timeout error"
    elif any(x in err_lower for x in ["model", "not found", "unknown model", "unsupported", "404"]):
        category = "Model unsupported/not found"
    elif any(x in err_lower for x in ["import", "package", "module", "dependency"]):
        category = "Package/import/configuration error"
    else:
        category = "Unknown provider error"

    return category, sanitized


def verify_llm_credentials_with_timeout(ai_model: str, api_key: str, api_base: str = "", timeout: float = 10.0) -> str | None:
    """Run verify_llm_credentials with a timeout."""
    from openoutreach.core.llm import verify_llm_credentials

    res = []
    def worker():
        try:
            res.append(verify_llm_credentials(ai_model, api_key, api_base))
        except Exception as e:
            res.append(str(e))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return "Connection/timeout error: LLM verification timed out."
    return res[0] if res else "Verification failed to complete."


class Command(BaseCommand):
    help = "Test LLM Configuration and print connection status."

    def handle(self, *args, **options):
        from openoutreach.core.models import SiteConfig

        cfg = SiteConfig.load()
        self.stdout.write("LLM Configuration Test")
        self.stdout.write("----------------------")
        self.stdout.write(f"Model: {cfg.ai_model or 'Not configured'}")
        self.stdout.write(f"Base URL: {cfg.llm_api_base or 'Not configured'}")
        
        status_str = cfg.get_llm_credential_state()
        self.stdout.write(f"API Key Status: {status_str}")

        if not cfg.ai_model or not cfg.is_llm_configured():
            self.stderr.write("Error: LLM is not configured (missing model or API key).")
            return

        self.stdout.write("Testing LLM credentials...")
        err = verify_llm_credentials_with_timeout(cfg.ai_model, cfg.llm_api_key, cfg.llm_api_base, timeout=10.0)
        
        if err is None:
            self.stdout.write("Success: LLM Configuration works correctly.")
        else:
            category, sanitized_msg = sanitize_and_classify_llm_error(err, cfg.llm_api_key)
            self.stderr.write(f"Failure Category: {category}")
            self.stderr.write(f"Error Details: {sanitized_msg}")
