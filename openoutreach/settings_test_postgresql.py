import os
from urllib.parse import urlparse, parse_qs
from openoutreach.settings import *

db_url = os.environ.get("DATABASE_URL", "postgresql:///huntflow_pg_gate")
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
        "NAME": url.path.lstrip("/") or "huntflow_pg_gate",
        "USER": url.username or "",
        "PASSWORD": url.password or "",
        "HOST": host_val,
        "PORT": str(url.port or 5432) if host_val else "",
        "OPTIONS": {
            "options": "-c lock_timeout=15000ms -c statement_timeout=60000ms",
        },
    }
}
SECRET_KEY = "test-secret-key-for-postgres-at-least-32-chars-long"
TOKEN_PEPPER = "test-token-pepper-1234567890-32chars-long-secret"

