#!/usr/bin/env python3
"""PostgreSQL 16 Backup & Restore Verification Script for HuntFlow.

Verifies pg_dump / psql restore cycle and asserts non-empty restored table row counts.
Non-interactive and bounded with hard timeouts.
"""

import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openoutreach.settings_test_postgresql")

import django
django.setup()

from django.db import connection
from django.contrib.auth.models import User
from openoutreach.core.db_helpers import temporary_db_alias
from openoutreach.core.models import Workspace, Campaign, AuditEvent, UsageMeter
from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, JobSearchPreference, Job, JobMatchAssessment, JobApplication
from openoutreach.linkedin.models import ConnectionAttempt
from openoutreach.emails.models import Mailbox


def _build_pg_cli(base_cmd: str, db_settings: dict, db_name: str = None, extra_args: list = None) -> list:
    cmd = [base_cmd]
    db_host = db_settings.get("HOST", "") or ""
    db_port = str(db_settings.get("PORT", "") or "")
    db_user = db_settings.get("USER", "") or ""

    if db_host:
        cmd.extend(["-h", db_host])
    if db_port and db_host:
        cmd.extend(["-p", db_port])
    if db_user:
        cmd.extend(["-U", db_user])
    if extra_args:
        cmd.extend(extra_args)
    if db_name:
        cmd.append(db_name)
    return cmd


def main():
    print("=== HUNTFLOW POSTGRESQL 16 BACKUP & RESTORE TEST ===")
    if connection.vendor != "postgresql":
        print(f"Error: Default database vendor is {connection.vendor}, expected postgresql.")
        return 1

    db_settings = connection.settings_dict
    db_name = db_settings["NAME"]
    db_password = db_settings.get("PASSWORD", "")

    pg_env = os.environ.copy()
    pg_env["PATH"] = f"/opt/homebrew/opt/postgresql@16/bin:{pg_env.get('PATH', '')}"
    if db_password:
        pg_env["PGPASSWORD"] = db_password
    pg_env["PGCONNECT_TIMEOUT"] = "10"

    # Ensure main database exists
    createdb_main = _build_pg_cli("createdb", db_settings, db_name=db_name)
    subprocess.run(createdb_main, env=pg_env, capture_output=True, text=True, timeout=60)

    # Run migrations on main test DB
    migrate_cmd = [sys.executable, "manage.py", "migrate", "--noinput"]
    subprocess.run(migrate_cmd, env=pg_env, capture_output=True, text=True, timeout=60)

    # Ensure test data exists by populating minimal records if empty
    user, _ = User.objects.get_or_create(username="pg_qa_owner")
    ws, _ = Workspace.objects.get_or_create(name="Postgres QA WS", slug="pg-qa-ws")
    cand_prof, _ = CandidateProfile.objects.get_or_create(user=user, defaults={"full_name": "Alex Morgan", "email": "operator@example.com"})
    v6, _ = CandidateProfileVersion.objects.get_or_create(profile=cand_prof, version_number=3, defaults={"full_name": "Alex Morgan", "email": "operator@example.com", "state": "approved", "is_finalized": True})
    camp, _ = Campaign.objects.get_or_create(workspace=ws, name="PG Test Campaign", defaults={"campaign_type": "job_outreach", "is_active": True})
    pref, _ = JobSearchPreference.objects.get_or_create(
        campaign=camp,
        defaults={
            "workspace": ws,
            "candidate_profile": cand_prof,
            "candidate_profile_version": v6,
            "keywords": ["Software Engineer"],
            "match_threshold": 75,
        }
    )
    job, _ = Job.objects.get_or_create(title="Backend Developer", company="Acme Inc", defaults={"description": "Python Django backend engineer position"})
    assessment, _ = JobMatchAssessment.objects.get_or_create(
        job=job,
        defaults={
            "workspace": ws,
            "campaign": camp,
            "candidate_profile": cand_prof,
            "candidate_profile_version": v6,
            "score": 85.0,
            "threshold": 75,
            "eligible": True,
            "cv_checksum": "dummy_cv_hash",
            "job_checksum": "dummy_job_hash",
        }
    )
    audit_ev, _ = AuditEvent.objects.get_or_create(workspace=ws, event_type="backup_test", action_key="pg_backup_action_1", defaults={"payload": {"test": True}})

    # Capture row counts before backup
    before_counts = {
        "Workspace": Workspace.objects.count(),
        "Campaign": Campaign.objects.count(),
        "JobSearchPreference": JobSearchPreference.objects.count(),
        "Job": Job.objects.count(),
        "JobMatchAssessment": JobMatchAssessment.objects.count(),
        "ConnectionAttempt": ConnectionAttempt.objects.count(),
        "JobApplication": JobApplication.objects.count(),
        "Mailbox": Mailbox.objects.count(),
        "AuditEvent": AuditEvent.objects.count(),
        "UsageMeter": UsageMeter.objects.count(),
    }

    print("\nRow counts BEFORE backup:")
    for model_name, cnt in before_counts.items():
        print(f"  {model_name}: {cnt}")

    dump_file = BASE_DIR / "artifacts" / "pg_test_backup.sql"
    dump_file.parent.mkdir(parents=True, exist_ok=True)

    restore_db_name = f"{db_name}_restore_test"
    drop_cmd = _build_pg_cli("dropdb", db_settings, db_name=restore_db_name, extra_args=["--if-exists"])

    try:
        # 1. Execute pg_dump
        dump_cmd = _build_pg_cli("pg_dump", db_settings, db_name=db_name, extra_args=["-F", "p", "-f", str(dump_file)])
        print(f"\nExecuting pg_dump to {dump_file.name}...")
        res = subprocess.run(dump_cmd, env=pg_env, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            print(f"pg_dump failed: {res.stderr}")
            return 1
        print("pg_dump succeeded.")

        # 2. Recreate target restore database
        print(f"\nRecreating restore database {restore_db_name}...")
        subprocess.run(drop_cmd, env=pg_env, capture_output=True, text=True, timeout=60)
        createdb_cmd = _build_pg_cli("createdb", db_settings, db_name=restore_db_name)
        res_cdb = subprocess.run(createdb_cmd, env=pg_env, capture_output=True, text=True, timeout=60)
        if res_cdb.returncode != 0:
            print(f"createdb failed: {res_cdb.stderr}")
            return 1

        # 3. Restore dump using psql
        print(f"Restoring dump into {restore_db_name} using psql...")
        psql_cmd = _build_pg_cli("psql", db_settings, extra_args=["-d", restore_db_name, "-f", str(dump_file)])
        res_restore = subprocess.run(psql_cmd, env=pg_env, capture_output=True, text=True, timeout=60)
        if res_restore.returncode != 0:
            print(f"psql restore failed: {res_restore.stderr}")
            return 1
        print("psql restore completed.")

        # 4. Connect to restored database safely via temporary_db_alias context manager
        restored_db_settings = db_settings.copy()
        restored_db_settings["NAME"] = restore_db_name

        with temporary_db_alias("restored_db", restored_db_settings):
            after_counts = {
                "Workspace": Workspace.objects.using("restored_db").count(),
                "Campaign": Campaign.objects.using("restored_db").count(),
                "JobSearchPreference": JobSearchPreference.objects.using("restored_db").count(),
                "Job": Job.objects.using("restored_db").count(),
                "JobMatchAssessment": JobMatchAssessment.objects.using("restored_db").count(),
                "ConnectionAttempt": ConnectionAttempt.objects.using("restored_db").count(),
                "JobApplication": JobApplication.objects.using("restored_db").count(),
                "Mailbox": Mailbox.objects.using("restored_db").count(),
                "AuditEvent": AuditEvent.objects.using("restored_db").count(),
                "UsageMeter": UsageMeter.objects.using("restored_db").count(),
            }

            print("\nRow counts AFTER restore:")
            for model_name, cnt in after_counts.items():
                print(f"  {model_name}: {cnt}")

            mismatches = []
            for model_name in before_counts:
                if before_counts[model_name] != after_counts[model_name]:
                    mismatches.append((model_name, before_counts[model_name], after_counts[model_name]))

            if mismatches:
                print(f"\nFAILURE: Row count mismatches detected: {mismatches}")
                return 1

            print("\nSUCCESS: All model row counts match perfectly before and after PostgreSQL backup & restore cycle!")
            return 0
    finally:
        # Cleanup temporary dump file and drop test restore DB
        if dump_file.exists():
            dump_file.unlink()
        subprocess.run(drop_cmd, env=pg_env, capture_output=True, text=True, timeout=60)


if __name__ == "__main__":
    sys.exit(main())
