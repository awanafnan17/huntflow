# openoutreach/settings.py
"""
Minimal Django settings for using DjangoCRM's ORM + admin.
"""
import os
import sys
from pathlib import Path

# Playwright's sync API runs inside an async event loop, which triggers
# Django's async-safety check. We only use the ORM synchronously, so this is safe.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

ROOT_DIR = Path(__file__).resolve().parent.parent

BASE_DIR = ROOT_DIR

# Load .env file if it exists in BASE_DIR
env_path = BASE_DIR / ".env"
if env_path.exists():
    with open(env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip("'").strip('"'))

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "openoutreach-local-dev-key-change-in-production")
TOKEN_PEPPER = os.environ.get("TOKEN_PEPPER", "")
HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY = os.environ.get("HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY", os.environ.get("HUNTFLOW_ENCRYPTION_KEY", ""))
HUNTFLOW_ENCRYPTION_KEY = os.environ.get("HUNTFLOW_ENCRYPTION_KEY", "")

DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")

_hosts = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
ALLOWED_HOSTS = [h.strip() for h in _hosts.split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.sites",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "openoutreach.crm.apps.CrmConfig",
    "openoutreach.chat.apps.ChatConfig",
    "openoutreach.core.apps.CoreConfig",
    "openoutreach.linkedin.apps.LinkedInConfig",
    "openoutreach.emails.apps.EmailsConfig",
    "openoutreach.dashboard.apps.DashboardConfig",
    "openoutreach.career.apps.CareerConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "openoutreach.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "openoutreach.dashboard.context_processors.inbox_badge_processor",
            ],
        },
    },
]

db_url = os.environ.get("DATABASE_URL")
if db_url and (db_url.startswith("postgresql://") or db_url.startswith("postgres://")):
    from urllib.parse import urlparse, parse_qs
    url = urlparse(db_url)
    query_params = parse_qs(url.query)
    host_val = url.hostname or ""
    if not host_val and "host" in query_params:
        host_val = query_params["host"][0]
    if host_val == "localhost" and not url.password and not url.port:
        host_val = ""
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": url.path.lstrip("/"),
            "USER": url.username or "",
            "PASSWORD": url.password or "",
            "HOST": host_val,
            "PORT": str(url.port or 5432) if host_val else "",
        }
    }

else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(ROOT_DIR / "data" / "db.sqlite3"),
            "OPTIONS": {
                "timeout": 60,
            },
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SITE_ID = 1

STATIC_URL = "/static/"
STATIC_ROOT = ROOT_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = ROOT_DIR / "media"
PRIVATE_MEDIA_ROOT = ROOT_DIR / "private_media"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

redis_url = os.environ.get("REDIS_URL")
if redis_url:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": redis_url,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "huntflow-local-cache",
        }
    }

LOGIN_URL = "/dashboard/login/"

AUTHENTICATION_BACKENDS = [
    "openoutreach.core.auth_backends.EmailOrUsernameModelBackend",
    "django.contrib.auth.backends.ModelBackend",
]

DEFAULT_FROM_EMAIL = "noreply@localhost"
EMAIL_SUBJECT_PREFIX = "CRM: "

LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English")]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

TESTING = sys.argv[1:2] == ["test"]

_csrf_origins = os.environ.get("CSRF_TRUSTED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1,http://localhost")
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_origins.split(",") if o.strip()]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if not DEBUG:
    is_test_env = "pytest" in sys.modules or TESTING
    SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "False").lower() in ("true", "1", "yes")
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "True" if SECURE_SSL_REDIRECT else "False").lower() in ("true", "1", "yes")
    CSRF_COOKIE_SECURE = os.environ.get("CSRF_COOKIE_SECURE", "True" if SECURE_SSL_REDIRECT else "False").lower() in ("true", "1", "yes")
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "31536000" if SECURE_SSL_REDIRECT else "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.environ.get("SECURE_HSTS_INCLUDE_SUBDOMAINS", "True" if SECURE_SSL_REDIRECT else "False").lower() in ("true", "1", "yes")
    SECURE_HSTS_PRELOAD = os.environ.get("SECURE_HSTS_PRELOAD", "True" if SECURE_SSL_REDIRECT else "False").lower() in ("true", "1", "yes")
    if not ("pytest" in sys.modules or TESTING):
        os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
