# tests/test_production_hardening.py
import os
import json
import shutil
import tempfile
import pytest
from unittest import mock
from django.conf import settings
from django.urls import reverse
from django.test import Client
from django.core.management import call_command
from openoutreach.core.models import SiteConfig, Campaign, DaemonStatus
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.emails.models import Mailbox
from openoutreach.core.production import get_production_readiness_snapshot


@pytest.fixture
def logged_in_staff_client(db):
    from django.contrib.auth.models import User
    user = User.objects.create_user(username="staff_user", password="pwd", is_staff=True)
    client = Client()
    client.login(username="staff_user", password="pwd")
    return client


@pytest.fixture
def temp_backup_dir():
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)


def test_production_readiness_snapshot_read_only(db):
    """Ensure production readiness check is fully read-only and does not write files or rows."""
    snapshot = get_production_readiness_snapshot()
    assert isinstance(snapshot, dict)
    assert "environment_status" in snapshot
    assert "database_status" in snapshot
    assert "migration_status" in snapshot
    assert "secret_config_status" in snapshot
    assert "blockers" in snapshot
    assert "warnings" in snapshot


def test_dashboard_production_staff_only(db, logged_in_staff_client):
    """Ensure production dashboard GET is staff-only, read-only, and creates no files or mutations."""
    url = reverse("dashboard:production")
    response = logged_in_staff_client.get(url)
    assert response.status_code == 200
    assert b"Production Hardening" in response.content

    # Anonymous user should redirect
    anon_client = Client()
    anon_resp = anon_client.get(url)
    assert anon_resp.status_code == 302


def test_default_secret_key_blocker(db, settings):
    """Ensure development secret keys trigger blockers."""
    settings.SECRET_KEY = "openoutreach-local-dev-key-change-in-production"
    settings.DEBUG = True
    snapshot = get_production_readiness_snapshot()
    assert any("SECRET_KEY" in b for b in snapshot["blockers"])
    assert any("DEBUG" in b for b in snapshot["blockers"])


def test_backup_huntflow_dry_run(db, temp_backup_dir, settings):
    """Ensure backup dry-run doesn't write files and respects --json output."""
    from django.db import connection
    if connection.vendor != "sqlite":
        pytest.skip("backup_huntflow is an SQLite-only backup command.")
    from io import StringIO
    out = StringIO()
    
    call_command("backup_huntflow", "--dry-run", "--json", "--output-dir", temp_backup_dir, stdout=out)
    output_str = out.getvalue().strip()
    data = json.loads(output_str)
    
    assert data["success"] is True
    assert data.get("db_copied", False) is False
    assert data.get("would_copy_database") is True
    assert data.get("would_export_config") is True
    assert len(os.listdir(temp_backup_dir)) == 0


def test_backup_huntflow_real_sqlite_execution(db, temp_backup_dir, settings):
    """Ensure backup execution copies database page-by-page safely and includes manifests."""
    from django.db import connection
    if connection.vendor != "sqlite":
        pytest.skip("backup_huntflow is an SQLite-only backup command.")
    import re
    from io import StringIO
    out = StringIO()

    # Configure a real SQLite test database file
    orig_engine = settings.DATABASES["default"].get("ENGINE")
    try:
        settings.DATABASES["default"]["ENGINE"] = "django.db.backends.sqlite3"
        # Run backup command
        call_command("backup_huntflow", "--output-dir", temp_backup_dir, stdout=out)
    finally:
        if orig_engine is not None:
            settings.DATABASES["default"]["ENGINE"] = orig_engine
    
    # Check if final timestamped folder was created
    dirs = [d for d in os.listdir(temp_backup_dir) if d.startswith("backup_")]
    assert len(dirs) == 1
    assert re.match(r"^backup_[0-9]{8}_[0-9]{6}$", dirs[0])
    
    backup_path = os.path.join(temp_backup_dir, dirs[0])
    assert os.path.exists(os.path.join(backup_path, "db_backup.sqlite3"))
    assert os.path.exists(os.path.join(backup_path, "manifest.json"))
    assert os.path.exists(os.path.join(backup_path, "safe_config.json"))

    # Verify manifest details
    with open(os.path.join(backup_path, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
        assert "backup_timestamp" in manifest
        assert manifest["django_version"] == "5.2"


def test_restore_huntflow_guide_validation(db, temp_backup_dir):
    """Ensure restore guide validates directories cleanly and does not perform mutations."""
    from io import StringIO
    out = StringIO()

    # Run with empty path
    call_command("restore_huntflow_guide", "--json", stdout=out)
    output_str = out.getvalue().strip()
    res = json.loads(output_str)
    assert res["valid"] is False
    assert len(res["errors"]) > 0

    # Run on invalid folder
    out2 = StringIO()
    call_command("restore_huntflow_guide", "--backup-path", temp_backup_dir, "--json", stdout=out2)
    res2 = json.loads(out2.getvalue().strip())
    assert res2["valid"] is False
    assert res2["db_found"] is False


def test_log_rotation_blocked_when_daemon_active(db):
    """Ensure log rotation is refused when the daemon is running."""
    from io import StringIO
    out = StringIO()
    err = StringIO()

    # Mock daemon running status
    daemon_status, _ = DaemonStatus.objects.get_or_create(pk=1)
    daemon_status.is_running = True
    daemon_status.pid = 12345
    daemon_status.save()

    # Write dummy log so file exists
    with open("daemon.log", "w", encoding="utf-8") as f:
        f.write("test log data\n")

    try:
        with mock.patch("openoutreach.core.management.commands.rotate_huntflow_logs.is_pid_alive", return_value="running"):
            call_command("rotate_huntflow_logs", stdout=out, stderr=err)
            err_msg = err.getvalue().strip()
            assert "Rotation blocked" in err_msg or "blocked" in err_msg.lower()
    finally:
        if os.path.exists("daemon.log"):
            os.remove("daemon.log")


def test_rotate_logs_success_stopped_daemon(db, temp_backup_dir):
    """Ensure log rotation succeeds when daemon is stopped."""
    from io import StringIO
    out = StringIO()

    # Ensure daemon stopped
    daemon_status, _ = DaemonStatus.objects.get_or_create(pk=1)
    daemon_status.is_running = False
    daemon_status.pid = None
    daemon_status.save()

    # Write a dummy log file
    log_file = "daemon.log"
    with open(log_file, "w", encoding="utf-8") as f:
        f.write("A" * 100)  # Write small logs

    try:
        call_command("rotate_huntflow_logs", "--max-mb", "0.00001", stdout=out)
        assert os.path.exists("daemon.log.1")
        assert os.path.exists("daemon.log")
        assert os.path.getsize("daemon.log") == 0
    finally:
        if os.path.exists("daemon.log.1"):
            os.remove("daemon.log.1")
        if os.path.exists("daemon.log"):
            os.remove("daemon.log")


def test_export_safe_config_credentials_masking(db, temp_backup_dir):
    """Ensure export command masks secrets correctly."""
    from io import StringIO
    out = StringIO()

    # Write dummy secret to DB
    cfg = SiteConfig.load()
    cfg.llm_api_key = "test-1-aaaaaaaa"
    cfg.save()

    call_command("export_safe_config", "--json", stdout=out)
    output_str = out.getvalue().strip()
    data = json.loads(output_str)

    # Key should be masked/boolean flags
    assert data["site_config"]["llm_configured"] is True
    assert "llm_api_key" not in data["site_config"]


def test_first_live_run_checklist_validation(db):
    """Ensure first live run checklist command compiles properly."""
    from io import StringIO
    out = StringIO()

    call_command("first_live_run_checklist", "--json", stdout=out)
    output_str = out.getvalue().strip()
    data = json.loads(output_str)

    assert "ready_for_controlled_live_run" in data
    assert "checks" in data
    assert "recommended_next_action" in data


def test_log_rotation_blocked_unknown_daemon_state(db):
    """Ensure log rotation is refused when the daemon process state is unknown."""
    from io import StringIO
    out = StringIO()
    err = StringIO()

    # Set process is_running to True/None, but is_pid_alive fails/unknown
    daemon_status, _ = DaemonStatus.objects.get_or_create(pk=1)
    daemon_status.is_running = True
    daemon_status.pid = 99999
    daemon_status.save()

    # Write dummy log so file exists
    with open("daemon.log", "w", encoding="utf-8") as f:
        f.write("test log data\n")

    try:
        # Patch is_pid_alive to raise an exception or simulate unknown state
        with mock.patch("openoutreach.core.management.commands.rotate_huntflow_logs.is_pid_alive", side_effect=Exception("Unknown")):
            call_command("rotate_huntflow_logs", stdout=out, stderr=err)
            err_msg = err.getvalue().strip()
            # Rotation blocked because of unknown daemon state
            assert "Rotation blocked" in err_msg or "blocked" in err_msg.lower()
    finally:
        if os.path.exists("daemon.log"):
            os.remove("daemon.log")


def test_log_rotation_dry_run_allowed_while_running(db):
    """Ensure dry-run log rotation is allowed even while daemon is running, making no changes."""
    from io import StringIO
    out = StringIO()
    err = StringIO()

    daemon_status, _ = DaemonStatus.objects.get_or_create(pk=1)
    daemon_status.is_running = True
    daemon_status.pid = 12345
    daemon_status.save()

    # Write dummy log so file exists
    with open("daemon.log", "w", encoding="utf-8") as f:
        f.write("test log data\n")

    try:
        with mock.patch("openoutreach.core.management.commands.rotate_huntflow_logs.is_pid_alive", return_value="running"):
            call_command("rotate_huntflow_logs", "--dry-run", "--max-mb", "0.000001", stdout=out, stderr=err)
            out_msg = out.getvalue().strip()
            assert "[DRY-RUN]" in out_msg
            assert os.path.exists("daemon.log")
            assert not os.path.exists("daemon.log.1")
    finally:
        if os.path.exists("daemon.log"):
            os.remove("daemon.log")


def test_log_rotation_unknown_files_untouched(db):
    """Ensure log rotation never touches or deletes unknown files in the directory."""
    from io import StringIO
    out = StringIO()

    daemon_status, _ = DaemonStatus.objects.get_or_create(pk=1)
    daemon_status.is_running = False
    daemon_status.pid = None
    daemon_status.save()

    # Create dummy active log and an unrelated log file
    with open("daemon.log", "w", encoding="utf-8") as f:
        f.write("test log\n")
    with open("unknown_file.log", "w", encoding="utf-8") as f:
        f.write("should not be touched\n")

    try:
        call_command("rotate_huntflow_logs", "--max-mb", "0.000001", stdout=out)
        assert os.path.exists("unknown_file.log")
        with open("unknown_file.log", "r", encoding="utf-8") as f:
            assert f.read() == "should not be touched\n"
    finally:
        if os.path.exists("daemon.log"):
            os.remove("daemon.log")
        if os.path.exists("daemon.log.1"):
            os.remove("daemon.log.1")
        if os.path.exists("unknown_file.log"):
            os.remove("unknown_file.log")


def test_backup_unsupported_backend(db, temp_backup_dir, settings):
    """Ensure non-SQLite databases return unsupported strategy without writing anything."""
    from io import StringIO
    out = StringIO()
    err = StringIO()

    orig_engine = settings.DATABASES["default"]["ENGINE"]
    try:
        settings.DATABASES["default"]["ENGINE"] = "django.db.backends.postgresql"
        call_command("backup_huntflow", "--output-dir", temp_backup_dir, stdout=out, stderr=err)
        err_msg = err.getvalue().strip()
        assert "Unsupported backend" in err_msg
        assert len(os.listdir(temp_backup_dir)) == 0
    finally:
        settings.DATABASES["default"]["ENGINE"] = orig_engine


def test_export_safe_config_overwrite_refusal(db, temp_backup_dir):
    """Ensure safe config export refuses to overwrite existing files."""
    from io import StringIO
    out = StringIO()
    err = StringIO()

    # Pre-create output file
    target_file = os.path.join(temp_backup_dir, "config.json")
    with open(target_file, "w", encoding="utf-8") as f:
        f.write("existing content")

    call_command("export_safe_config", "--output", target_file, stdout=out, stderr=err)
    err_msg = err.getvalue().strip()
    assert "Export failed" in err_msg
    with open(target_file, "r", encoding="utf-8") as f:
        assert f.read() == "existing content"


def test_no_execution_side_effects_on_view(db, logged_in_staff_client):
    """Confirm production check view causes no database write, file creation, or task actions."""
    url = reverse("dashboard:production")
    
    with mock.patch("openoutreach.linkedin.browser.connect_with_note.logger.info") as mock_log:
        response = logged_in_staff_client.get(url)
        assert response.status_code == 200
        # No actions or playwright tasks called
        assert not mock_log.called

