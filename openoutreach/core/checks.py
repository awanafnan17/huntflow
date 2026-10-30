# openoutreach/core/checks.py
from django.core.checks import Error, Tags, register
from django.conf import settings

DOCUMENTED_PLACEHOLDERS = {
    "openoutreach_token_pepper",
    "change_me_to_a_secure_token_pepper_secret_key_32bytes",
    "change_me_token_pepper",
    "change-me",
    "token_pepper_placeholder",
}


@register(Tags.security)
def check_token_pepper_security(app_configs, **kwargs):
    errors = []
    pepper = getattr(settings, "TOKEN_PEPPER", "")
    secret_key = getattr(settings, "SECRET_KEY", "")

    if not pepper:
        errors.append(
            Error(
                "TOKEN_PEPPER setting is missing or empty.",
                hint="Set the TOKEN_PEPPER environment variable with at least 32 characters of key material.",
                id="openoutreach.E001",
            )
        )
    elif len(pepper) < 32:
        errors.append(
            Error(
                "TOKEN_PEPPER is too short (must be at least 32 characters).",
                hint="Provide a TOKEN_PEPPER string containing at least 32 characters.",
                id="openoutreach.E002",
            )
        )
    elif pepper == secret_key:
        errors.append(
            Error(
                "TOKEN_PEPPER equals SECRET_KEY.",
                hint="TOKEN_PEPPER must be a dedicated secret distinct from SECRET_KEY.",
                id="openoutreach.E003",
            )
        )
    elif pepper.lower() in DOCUMENTED_PLACEHOLDERS:
        errors.append(
            Error(
                "TOKEN_PEPPER uses a documented placeholder value.",
                hint="Set a unique TOKEN_PEPPER secret for your deployment.",
                id="openoutreach.E004",
            )
        )

    return errors
