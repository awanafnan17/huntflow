import copy
import os
from contextlib import contextmanager
from django.conf import settings
from django.db import connections, close_old_connections


@contextmanager
def temporary_db_alias(alias, db_settings):
    """
    Safely registers a temporary database alias and guarantees complete cleanup
    of settings.DATABASES, connections.databases, connections.settings,
    connections._connections, environment variables, and open sockets upon exit.
    """
    original_databases = copy.deepcopy(settings.DATABASES)
    original_database_url = os.environ.get("DATABASE_URL")
    
    # 1. Update settings and connection registries
    settings.DATABASES[alias] = db_settings
    connections.databases[alias] = db_settings
    if hasattr(connections, "_settings") and isinstance(connections._settings, dict):
        connections._settings[alias] = db_settings

    try:
        yield alias
    finally:
        # 2. Close active connection for alias if present
        try:
            conn = getattr(getattr(connections, "_connections", None), alias, None)
            if conn is not None:
                conn.close()
        except Exception:
            pass

        try:
            if alias in connections:
                connections[alias].close()
        except Exception:
            pass

        # 3. Remove alias from connection registries
        if hasattr(connections, "_connections") and hasattr(connections._connections, alias):
            try:
                delattr(connections._connections, alias)
            except Exception:
                pass

        connections.databases.pop(alias, None)
        if hasattr(connections, "_settings") and isinstance(connections._settings, dict):
            connections._settings.pop(alias, None)

        # 4. Restore original settings and environment
        settings.DATABASES.clear()
        settings.DATABASES.update(original_databases)

        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url

        close_old_connections()
