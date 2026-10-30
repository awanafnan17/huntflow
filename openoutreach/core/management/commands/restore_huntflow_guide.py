# openoutreach/core/management/commands/restore_huntflow_guide.py
"""Restore guide helper command."""
from __future__ import annotations

import os
import json
from django.core.management.base import BaseCommand
from openoutreach.core.operator_alerts import mask_alert_payload


class Command(BaseCommand):
    help = "Validates backup directory assets and prints safe manual restore instructions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--backup-path",
            type=str,
            default="",
            help="Path to the timestamped backup directory to inspect.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output only valid JSON validation metrics to stdout.",
        )

    def handle(self, *args, **options):
        backup_path = options["backup_path"].strip()
        use_json = options["json"]

        errors = []
        warnings = []
        validation = {
            "valid": False,
            "backup_path": backup_path,
            "manifest_found": False,
            "db_found": False,
            "media_found": False,
            "manifest_details": {},
            "warnings": warnings,
            "errors": errors,
        }

        if not backup_path:
            msg = "Please provide an explicit backup path using --backup-path."
            errors.append(msg)
            if use_json:
                self.stdout.write(json.dumps(validation, default=str, indent=2))
            else:
                self.stderr.write(msg)
            return

        if not os.path.exists(backup_path):
            msg = f"Backup path does not exist: {backup_path}"
            errors.append(msg)
            if use_json:
                self.stdout.write(json.dumps(validation, default=str, indent=2))
            else:
                self.stderr.write(msg)
            return

        # Validate elements inside backup_path
        manifest_file = os.path.join(backup_path, "manifest.json")
        db_file = os.path.join(backup_path, "db_backup.sqlite3")
        media_dir = os.path.join(backup_path, "media")

        if os.path.exists(manifest_file):
            validation["manifest_found"] = True
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest_data = json.load(f)
                    validation["manifest_details"] = mask_alert_payload(manifest_data)
            except Exception as e:
                warnings.append(f"Failed to parse manifest.json: {e}")
        else:
            warnings.append("manifest.json not found in this backup directory.")

        if os.path.exists(db_file):
            validation["db_found"] = True
        else:
            errors.append("db_backup.sqlite3 database file not found in this backup directory.")

        if os.path.exists(media_dir) and os.path.isdir(media_dir):
            validation["media_found"] = True

        if not errors:
            validation["valid"] = True

        if use_json:
            self.stdout.write(json.dumps(validation, default=str, indent=2))
        else:
            self.stdout.write("=============================================================")
            self.stdout.write("HUNTFLOW RESTORE GUIDE & DIAGNOSTICS")
            self.stdout.write("=============================================================")
            self.stdout.write(f"Backup Directory: {backup_path}")
            self.stdout.write(f"Database Found  : {validation['db_found']}")
            self.stdout.write(f"Manifest Found  : {validation['manifest_found']}")
            
            if validation["manifest_found"]:
                details = validation["manifest_details"]
                self.stdout.write(f"  Backup Time   : {details.get('backup_timestamp', '-')}")
                self.stdout.write(f"  Media Copied  : {details.get('media_included', False)}")
                
            self.stdout.write(f"Validation      : {'PASSED' if validation['valid'] else 'FAILED'}")
            
            if errors:
                self.stdout.write(f"Errors          : {errors}")
            if warnings:
                self.stdout.write(f"Warnings        : {warnings}")
                
            if validation["valid"]:
                self.stdout.write("-------------------------------------------------------------")
                self.stdout.write("SAFE MANUAL RESTORE INSTRUCTIONS:")
                self.stdout.write("1. Stop the HuntFlow daemon process if it is running.")
                self.stdout.write("2. Make a copy of your active db.sqlite3 database for safety.")
                self.stdout.write(f"3. Copy '{db_file}' to your active 'data/db.sqlite3' path.")
                if validation["media_found"]:
                    self.stdout.write(f"4. Restore media files from '{media_dir}' back to your 'media/' folder.")
                self.stdout.write("5. Run Django checks and launch previews before restarting the daemon.")
                self.stdout.write("WARNING: Database overwrite will overwrite all current campaign progress.")
            self.stdout.write("=============================================================")
