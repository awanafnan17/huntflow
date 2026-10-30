import os
from openoutreach.settings import *

DEBUG = False
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "True").lower() in ("true", "1", "yes")
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "True").lower() in ("true", "1", "yes")
CSRF_COOKIE_SECURE = os.environ.get("CSRF_COOKIE_SECURE", "True").lower() in ("true", "1", "yes")
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.environ.get("SECURE_HSTS_INCLUDE_SUBDOMAINS", "True").lower() in ("true", "1", "yes")
SECURE_HSTS_PRELOAD = os.environ.get("SECURE_HSTS_PRELOAD", "True").lower() in ("true", "1", "yes")
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "https://localhost,https://127.0.0.1").split(",") if o.strip()]
