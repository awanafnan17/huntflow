import os
from urllib.parse import urlparse
from openoutreach.settings import *

DEBUG = False
db_url = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/huntflow_pg_gate")
url = urlparse(db_url)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": url.path.lstrip("/") or "huntflow_pg_gate",
        "USER": url.username or "",
        "PASSWORD": url.password or "",
        "HOST": url.hostname or "localhost",
        "PORT": str(url.port or 5432),
    }
}

ALLOWED_HOSTS = ["*"]
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
