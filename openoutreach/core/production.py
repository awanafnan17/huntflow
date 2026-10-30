# openoutreach/core/production.py
from __future__ import annotations

import os
from django.conf import settings
from django.utils import timezone
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from openoutreach.core.models import SiteConfig, Campaign, DaemonStatus
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.emails.models import Mailbox
from openoutreach.core.readiness import check_launch_readiness, is_pid_alive
from openoutreach.core.operator_alerts import get_operator_alert_summary


def get_production_readiness_snapshot() -> dict:
    """Read-only production configuration, secret keys, migration, and filesystem checks."""
    blockers = []
    warnings = []
    info_items = []

    # 1. Environment Check
    debug_mode = getattr(settings, "DEBUG", True)
    if debug_mode:
        blockers.append("DEBUG mode is active in settings (must be disabled for production).")
    
    secret_key = getattr(settings, "SECRET_KEY", "")
    is_default_key = secret_key == "openoutreach-local-dev-key-change-in-production"
    if not secret_key:
        blockers.append("Django SECRET_KEY is empty.")
    elif is_default_key:
        blockers.append("Django SECRET_KEY is set to the default development value.")
    elif len(secret_key) < 20:
        blockers.append("Django SECRET_KEY is insecure (too short).")

    allowed_hosts = getattr(settings, "ALLOWED_HOSTS", [])
    if not debug_mode:
        if not allowed_hosts:
            blockers.append("ALLOWED_HOSTS is empty while DEBUG=False.")
        elif "*" in allowed_hosts:
            warnings.append("ALLOWED_HOSTS contains a wildcard * wildcard in a production setting.")

    # 2. Database & Migration Check
    db_conn = connections["default"]
    db_accessible = True
    db_backend = db_conn.settings_dict.get("ENGINE", "")
    
    try:
        db_conn.ensure_connection()
    except Exception as e:
        db_accessible = False
        blockers.append(f"Database connection failed: {e}")

    unapplied_migrations_count = 0
    if db_accessible:
        try:
            executor = MigrationExecutor(db_conn)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
            unapplied_migrations_count = len(plan)
            if unapplied_migrations_count > 0:
                blockers.append(f"Database has {unapplied_migrations_count} unapplied migrations.")
        except Exception as e:
            blockers.append(f"Failed to inspect database migrations: {e}")

    # 3. Secret Configuration Presence Checks (Non-leaking)
    cfg = SiteConfig.load()
    llm_configured = bool(cfg.llm_api_key and cfg.ai_model)
    bettercontact_configured = bool(cfg.bettercontact_api_key)
    
    if not llm_configured:
        blockers.append("LLM provider configurations (API key or model identifier) are missing.")
    if not bettercontact_configured:
        warnings.append("BetterContact API key is unconfigured (emails finder will not resolve).")

    # 4. LinkedIn Account Status
    active_profile = LinkedInProfile.objects.filter(active=True).first()
    has_active_linkedin = active_profile is not None
    linkedin_credentials_valid = False
    
    if not has_active_linkedin:
        blockers.append("No active LinkedInProfile config exists in the database.")
    else:
        linkedin_username = active_profile.linkedin_username
        linkedin_password = active_profile.linkedin_password
        if not linkedin_username or not linkedin_password:
            blockers.append("Active LinkedInProfile is missing a username or password.")
        else:
            linkedin_credentials_valid = True

    # 5. Mailbox Status
    has_mailbox = Mailbox.objects.exists()
    if not has_mailbox:
        warnings.append("No SMTP mailboxes are configured in the system.")

    # 6. Campaign Gating
    has_active_campaign = Campaign.objects.filter(is_active=True).exists()
    if not has_active_campaign:
        blockers.append("There are no active campaigns registered in the database.")

    # 7. Launch Readiness Summary
    readiness = check_launch_readiness()
    if readiness.get("overall_status") == "blocked":
        blockers.append("Standard outreach launch readiness diagnostics are currently blocked.")

    # 8. Daemon Process State (Safety check)
    daemon_status, _ = DaemonStatus.objects.get_or_create(pk=1)
    process_state = is_pid_alive(daemon_status.pid)
    
    # 9. Operator Inbox Alerts
    inbox_summary = get_operator_alert_summary()
    active_alerts_count = inbox_summary.get("active", 0)
    if active_alerts_count > 0:
        warnings.append(f"There are {active_alerts_count} active unacknowledged alert(s) in the Operator Inbox.")

    # 10. Backup Location & Filesystem Gating
    # We inspect settings or direct directories
    db_path = db_conn.settings_dict.get("NAME", "")
    db_dir_writable = False
    
    if db_path and db_path != ":memory:":
        db_dir = os.path.dirname(db_path)
        if os.path.isdir(db_dir) and os.access(db_dir, os.W_OK):
            db_dir_writable = True
        else:
            blockers.append("Database storage directory is not writeable by the active system process.")
    else:
        # In-memory databases are a blocker for safe go-live
        blockers.append("Database is configured as an in-memory instance or has an invalid storage path.")

    # Check daemon.log writeability
    log_file_writable = False
    if os.path.exists("daemon.log"):
        if os.access("daemon.log", os.W_OK):
            log_file_writable = True
        else:
            blockers.append("Active log file (daemon.log) exists but is not writeable.")
    else:
        # Check current directory writeability for log creation
        if os.access(".", os.W_OK):
            log_file_writable = True
        else:
            blockers.append("Log directory path is not writeable.")

    # Backup checks
    backup_dir = os.path.join("data", "backups")
    backup_dir_exists = os.path.exists(backup_dir)
    backup_count = 0
    if backup_dir_exists:
        for _root, _dirs, _files in os.walk(backup_dir):
            for _f in _files:
                if _f.endswith(".sqlite3"):
                    backup_count += 1
        
    if backup_count == 0:
        warnings.append("No database backup files found. Run the backup command before go-live.")

    # 11. Compile snapshot dict
    environment_status = {
        "debug": debug_mode,
        "secret_key_configured": bool(secret_key and not is_default_key),
        "allowed_hosts": allowed_hosts,
    }
    
    database_status = {
        "engine": db_backend,
        "accessible": db_accessible,
        "writeable": db_dir_writable,
    }

    backup_status = {
        "directory_exists": backup_dir_exists,
        "backup_count": backup_count,
        "backup_dir_path": backup_dir,
    }

    log_status = {
        "daemon_log_exists": os.path.exists("daemon.log"),
        "daemon_log_size_bytes": os.path.getsize("daemon.log") if os.path.exists("daemon.log") else 0,
        "log_writable": log_file_writable,
    }

    # Recommended next action
    if blockers:
        next_action = f"Resolve production blockers: {blockers[0]}"
    elif warnings:
        next_action = f"Review production warnings: {warnings[0]}"
    else:
        next_action = "System is ready for controlled live outreach launch."

    return {
        "environment_status": environment_status,
        "database_status": database_status,
        "migration_status": {
            "unapplied_count": unapplied_migrations_count,
        },
        "secret_config_status": {
            "llm_configured": llm_configured,
            "bettercontact_configured": bettercontact_configured,
        },
        "linkedin_account_status": {
            "active_linkedin": has_active_linkedin,
            "credentials_valid": linkedin_credentials_valid,
        },
        "mailbox_status": {
            "has_mailbox": has_mailbox,
        },
        "daemon_status": {
            "is_running": daemon_status.is_running,
            "pid": daemon_status.pid,
            "process_state": process_state,
        },
        "launch_readiness_status": {
            "overall_status": readiness.get("overall_status"),
        },
        "operations_alert_status": {
            "active_alerts_count": active_alerts_count,
        },
        "backup_status": backup_status,
        "log_status": log_status,
        "blockers": blockers,
        "warnings": warnings,
        "info_items": info_items,
        "recommended_next_action": next_action,
    }
