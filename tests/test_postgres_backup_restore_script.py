import copy
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from django.conf import settings
from django.db import connections

import scripts.test_postgres_backup_restore as backup_script


@pytest.mark.django_db
def test_postgres_backup_restore_script_imports_and_models():
    """Verify that backup script imports all required models cleanly and defines main."""
    assert hasattr(backup_script, "main")
    assert callable(backup_script.main)
    assert backup_script.Workspace is not None
    assert backup_script.Campaign is not None
    assert backup_script.Job is not None


@pytest.mark.django_db
def test_postgres_backup_restore_script_sqlite_non_pg_bail():
    """Verify script bails cleanly with error code when connection vendor is not postgresql."""
    with patch("scripts.test_postgres_backup_restore.connection") as mock_conn:
        mock_conn.vendor = "sqlite"
        res = backup_script.main()
        assert res == 1


@pytest.mark.django_db
def test_postgres_backup_restore_script_execution_mocked_pg():
    """Verify complete backup and restore script flow with mocked subprocess calls."""
    orig_db_keys = set(settings.DATABASES.keys())
    orig_conn_keys = set(connections.databases.keys())
    orig_engine = settings.DATABASES["default"]["ENGINE"]
    orig_name = settings.DATABASES["default"]["NAME"]
    orig_env_db_url = os.environ.get("DATABASE_URL")
    orig_env_settings = os.environ.get("DJANGO_SETTINGS_MODULE")

    with patch("scripts.test_postgres_backup_restore.connection") as mock_conn, \
         patch("scripts.test_postgres_backup_restore.subprocess.run") as mock_run, \
         patch.object(backup_script.Workspace.objects, "using") as mock_ws_using, \
         patch.object(backup_script.Campaign.objects, "using") as mock_camp_using, \
         patch.object(backup_script.JobSearchPreference.objects, "using") as mock_jsp_using, \
         patch.object(backup_script.Job.objects, "using") as mock_job_using, \
         patch.object(backup_script.JobMatchAssessment.objects, "using") as mock_jma_using, \
         patch.object(backup_script.ConnectionAttempt.objects, "using") as mock_ca_using, \
         patch.object(backup_script.JobApplication.objects, "using") as mock_ja_using, \
         patch.object(backup_script.Mailbox.objects, "using") as mock_mb_using, \
         patch.object(backup_script.AuditEvent.objects, "using") as mock_ae_using, \
         patch.object(backup_script.UsageMeter.objects, "using") as mock_um_using:

        mock_conn.vendor = "postgresql"
        mock_conn.settings_dict = {
            "NAME": "test_db",
            "USER": "postgres",
            "HOST": "localhost",
            "PORT": 5432,
        }
        
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_run.return_value = mock_res
        
        # Set up mock counts for using("restored_db")
        for m in (mock_ws_using, mock_camp_using, mock_jsp_using, mock_job_using, mock_jma_using, mock_ae_using):
            m.return_value.count.return_value = 1
        for m in (mock_ca_using, mock_ja_using, mock_mb_using, mock_um_using):
            m.return_value.count.return_value = 0
        
        res = backup_script.main()
        assert res == 0
        assert mock_run.called

    # Required assertions after success
    assert "restored_db" not in connections.databases
    assert "restored_db" not in settings.DATABASES
    assert set(settings.DATABASES.keys()) == orig_db_keys
    assert set(connections.databases.keys()) == orig_conn_keys
    assert settings.DATABASES["default"]["ENGINE"] == orig_engine
    assert settings.DATABASES["default"]["NAME"] == orig_name
    assert os.environ.get("DATABASE_URL") == orig_env_db_url
    assert os.environ.get("DJANGO_SETTINGS_MODULE") == orig_env_settings


@pytest.mark.parametrize("fail_point", ["seed", "pg_dump", "restore", "mismatch"])
@pytest.mark.django_db
def test_postgres_backup_restore_script_isolation_on_failure(fail_point):
    """Verify restored_db is absent and state is restored on any failure point."""
    orig_db_keys = set(settings.DATABASES.keys())
    orig_conn_keys = set(connections.databases.keys())
    orig_engine = settings.DATABASES["default"]["ENGINE"]
    orig_name = settings.DATABASES["default"]["NAME"]
    orig_env_db_url = os.environ.get("DATABASE_URL")
    orig_env_settings = os.environ.get("DJANGO_SETTINGS_MODULE")

    with patch("scripts.test_postgres_backup_restore.connection") as mock_conn, \
         patch("scripts.test_postgres_backup_restore.subprocess.run") as mock_run, \
         patch.object(backup_script.Workspace.objects, "using") as mock_ws_using, \
         patch.object(backup_script.Campaign.objects, "using") as mock_camp_using, \
         patch.object(backup_script.JobSearchPreference.objects, "using") as mock_jsp_using, \
         patch.object(backup_script.Job.objects, "using") as mock_job_using, \
         patch.object(backup_script.JobMatchAssessment.objects, "using") as mock_jma_using, \
         patch.object(backup_script.ConnectionAttempt.objects, "using") as mock_ca_using, \
         patch.object(backup_script.JobApplication.objects, "using") as mock_ja_using, \
         patch.object(backup_script.Mailbox.objects, "using") as mock_mb_using, \
         patch.object(backup_script.AuditEvent.objects, "using") as mock_ae_using, \
         patch.object(backup_script.UsageMeter.objects, "using") as mock_um_using:

        mock_conn.vendor = "postgresql"
        mock_conn.settings_dict = {
            "NAME": "test_db",
            "USER": "postgres",
            "HOST": "localhost",
            "PORT": 5432,
        }

        if fail_point == "seed":
            with patch.object(backup_script.Workspace.objects, "get_or_create", side_effect=Exception("Seed failure")):
                with pytest.raises(Exception, match="Seed failure"):
                    backup_script.main()
        elif fail_point == "pg_dump":
            def mock_sub_run(cmd, **kwargs):
                res = MagicMock()
                if "pg_dump" in cmd:
                    res.returncode = 1
                    res.stderr = "pg_dump failed"
                else:
                    res.returncode = 0
                return res
            mock_run.side_effect = mock_sub_run
            res = backup_script.main()
            assert res == 1
        elif fail_point == "restore":
            def mock_sub_run(cmd, **kwargs):
                res = MagicMock()
                if "psql" in cmd:
                    res.returncode = 1
                    res.stderr = "psql restore failed"
                else:
                    res.returncode = 0
                return res
            mock_run.side_effect = mock_sub_run
            res = backup_script.main()
            assert res == 1
        elif fail_point == "mismatch":
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_run.return_value = mock_res
            for m in (mock_ws_using, mock_camp_using, mock_jsp_using, mock_job_using, mock_jma_using, mock_ae_using, mock_ca_using, mock_ja_using, mock_mb_using, mock_um_using):
                m.return_value.count.return_value = 999  # count mismatch!
            res = backup_script.main()
            assert res == 1

    # State assertions on failure
    assert "restored_db" not in connections.databases
    assert "restored_db" not in settings.DATABASES
    assert set(settings.DATABASES.keys()) == orig_db_keys
    assert set(connections.databases.keys()) == orig_conn_keys
    assert settings.DATABASES["default"]["ENGINE"] == orig_engine
    assert settings.DATABASES["default"]["NAME"] == orig_name
    assert os.environ.get("DATABASE_URL") == orig_env_db_url
    assert os.environ.get("DJANGO_SETTINGS_MODULE") == orig_env_settings
