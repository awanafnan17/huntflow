# openoutreach/core/management/commands/backup_huntflow.py
"""Database backup management command.

Uses SQLite Online Backup API for transaction-consistent copies.
"""
from __future__ import annotations

import os
import shutil
import json
import sqlite3
from datetime import datetime
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from openoutreach.core.operator_alerts import mask_alert_payload


class Command(BaseCommand):
    help = "Generate a transaction-consistent SQLite database backup and safe configuration snapshot."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            type=str,
            default="",
            help="Alternative output parent directory. Default is data/backups/.",
        )
        parser.add_argument(
            "--include-media",
            action="store_true",
            help="Include media files in the backup directory.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output only valid JSON configuration summary to stdout.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simulate the backup workflow without performing write actions.",
        )

    def handle(self, *args, **options):
        output_parent = options["output_dir"].strip()
        include_media = options["include_media"]
        use_json = options["json"]
        dry_run = options["dry_run"]

        errors = []
        warnings = []
        metrics = {
            "success": False,
            "backup_dir": None,
            "db_copied": False,
            "config_exported": False,
            "media_included": False,
            "warnings": warnings,
            "errors": errors,
        }

        # 1. Database Backend Check
        db_config = settings.DATABASES.get("default", {})
        engine = db_config.get("ENGINE", "")
        db_path = db_config.get("NAME", "")

        if "sqlite3" not in engine:
            msg = "Unsupported backend: Database backup is supported only for SQLite3."
            errors.append(msg)
            metrics["strategy"] = "Export database using your engine's custom dump tool (e.g. pg_dump for PostgreSQL)."
            if use_json:
                self.stdout.write(json.dumps(metrics, default=str, indent=2))
            else:
                self.stderr.write(msg)
            return

        if db_path == ":memory:":
            msg = "Backup failed: Filesystem backups are unsupported for in-memory SQLite instances."
            errors.append(msg)
            if use_json:
                self.stdout.write(json.dumps(metrics, default=str, indent=2))
            else:
                self.stderr.write(msg)
            return

        # Prepare directory names
        if not output_parent:
            output_parent = os.path.join(settings.BASE_DIR, "data", "backups")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        staging_dir = os.path.join(output_parent, f"staging_backup_{timestamp}")
        final_dir = os.path.join(output_parent, f"backup_{timestamp}")

        metrics["backup_dir"] = final_dir

        if dry_run:
            metrics["success"] = True
            metrics["would_copy_database"] = True
            metrics["would_export_config"] = True
            metrics["would_include_media"] = include_media
            if use_json:
                self.stdout.write(json.dumps(metrics, default=str, indent=2))
            else:
                self.stdout.write(f"[DRY-RUN] Backup would be saved to: {final_dir}")
            return

        # Create staging path
        try:
            os.makedirs(staging_dir, exist_ok=True)
        except Exception as e:
            msg = f"Failed to create backup directory: {e}"
            errors.append(msg)
            if use_json:
                self.stdout.write(json.dumps(metrics, default=str, indent=2))
            else:
                self.stderr.write(msg)
            return

        # Perform transaction-consistent SQLite Online Backup
        src_conn = None
        dst_conn = None
        staging_db_path = os.path.join(staging_dir, "db_backup.sqlite3")
        
        try:
            # Confirm source and destination are different
            if os.path.abspath(db_path) == os.path.abspath(staging_db_path):
                raise ValueError("Source and destination paths are identical.")

            # Connect to database files
            src_conn = sqlite3.connect(db_path)
            dst_conn = sqlite3.connect(staging_db_path)
            
            # Execute online backup
            src_conn.backup(dst_conn)
            metrics["db_copied"] = True
        except Exception as e:
            errors.append(f"SQLite backup failed: {e}")
        finally:
            if dst_conn:
                dst_conn.close()
            if src_conn:
                src_conn.close()

        # Write safe configuration snapshot, manifest, and copy media
        if not errors:
            try:
                # 1. Config Export (Credential masked)
                from openoutreach.core.production import get_production_readiness_snapshot
                snapshot = get_production_readiness_snapshot()
                
                # Strip raw secrets
                clean_snapshot = mask_alert_payload(snapshot)
                
                config_path = os.path.join(staging_dir, "safe_config.json")
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(clean_snapshot, f, indent=2, default=str)
                metrics["config_exported"] = True

                # 2. Migration states log
                migration_log_path = os.path.join(staging_dir, "migration_state.json")
                migration_data = {
                    "migration_status": clean_snapshot.get("migration_status", {}),
                    "timestamp": timestamp
                }
                with open(migration_log_path, "w", encoding="utf-8") as f:
                    json.dump(migration_data, f, indent=2)

                # 3. Media files
                if include_media:
                    media_src = os.path.join(settings.BASE_DIR, "media")
                    if os.path.exists(media_src):
                        media_dst = os.path.join(staging_dir, "media")
                        shutil.copytree(media_src, media_dst)
                        metrics["media_included"] = True
                    else:
                        warnings.append("Media directory does not exist; skipping media copy.")

                # 4. Manifest
                manifest_path = os.path.join(staging_dir, "manifest.json")
                manifest_data = {
                    "backup_timestamp": timestamp,
                    "media_included": include_media and metrics["media_included"],
                    "django_version": "5.2",
                    "files_count": len(os.listdir(staging_dir)),
                }
                with open(manifest_path, "w", encoding="utf-8") as f:
                    json.dump(manifest_data, f, indent=2)

                # Finalize staging directory
                shutil.move(staging_dir, final_dir)
                metrics["success"] = True
            except Exception as e:
                errors.append(f"Backup finalization failed: {e}")
                # Cleanup staging folder on failure
                if os.path.exists(staging_dir):
                    shutil.rmtree(staging_dir)

        if use_json:
            self.stdout.write(json.dumps(metrics, default=str, indent=2))
        else:
            if metrics["success"]:
                self.stdout.write("=============================================================")
                self.stdout.write("HUNTFLOW DATABASE BACKUP SUCCESSFUL")
                self.stdout.write("=============================================================")
                self.stdout.write(f"Backup Folder : {final_dir}")
                self.stdout.write(f"SQLite DB File: db_backup.sqlite3")
                self.stdout.write(f"Safe Config   : safe_config.json")
                self.stdout.write(f"Media Included: {metrics['media_included']}")
                if warnings:
                    self.stdout.write(f"Warnings: {warnings}")
                self.stdout.write("=============================================================")
            else:
                self.stderr.write(f"Backup failed: {errors}")
