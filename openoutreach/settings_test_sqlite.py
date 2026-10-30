from openoutreach.settings import *

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(ROOT_DIR / "data" / "db.sqlite3"),
    }
}
SECRET_KEY = "test-secret-key-for-sqlite-testing-32chars"
TOKEN_PEPPER = "test-token-pepper-1234567890-32chars-long-secret"
