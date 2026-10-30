# openoutreach/core/management/commands/export_safe_config.py
"""Config export management command."""
from __future__ import annotations

import os
import json
from django.core.management.base import BaseCommand
from openoutreach.core.models import SiteConfig, Campaign
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.emails.models import Mailbox
from openoutreach.core.readiness import check_launch_readiness
from openoutreach.core.operator_alerts import mask_alert_payload


class Command(BaseCommand):
    help = "Exports credential-safe environment configs and settings diagnostics."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=str,
            default="",
            help="File path to write the configuration to. Default is stdout.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output only valid JSON configuration summary to stdout.",
        )

    def handle(self, *args, **options):
        output_path = options["output"].strip()
        use_json = options["json"]

        errors = []
        warnings = []
        metrics = {
            "success": False,
            "exported_to_file": False,
            "warnings": warnings,
            "errors": errors,
            "data": {},
        }

        # Refuse to overwrite files
        if output_path and os.path.exists(output_path):
            msg = f"Export failed: The output file path already exists: {output_path}"
            errors.append(msg)
            if use_json:
                self.stdout.write(json.dumps(metrics, default=str, indent=2))
            else:
                self.stderr.write(msg)
            return

        try:
            # 1. SiteConfig details
            cfg = SiteConfig.load()
            site_data = {
                "ai_model": cfg.ai_model,
                "llm_configured": bool(cfg.llm_api_key),
                "bettercontact_configured": bool(cfg.bettercontact_api_key),
            }

            # 2. LinkedIn active account status
            profile = LinkedInProfile.objects.filter(active=True).first()
            linkedin_data = {
                "active_profile": bool(profile),
                "linkedin_username": profile.linkedin_username if profile else "",
                "connect_limit": profile.connect_daily_limit if profile else 0,
                "follow_up_limit": profile.follow_up_daily_limit if profile else 0,
            }

            # 3. Mailboxes status
            mailboxes = []
            for box in Mailbox.objects.all():
                mailboxes.append({
                    "username": box.username,
                    "host": box.host,
                    "port": box.port,
                    "limit": box.daily_limit,
                })

            # 4. Campaign settings
            campaigns = []
            for cmp in Campaign.objects.all():
                campaigns.append({
                    "name": cmp.name,
                    "is_active": cmp.is_active,
                    "objective": cmp.campaign_objective,
                })

            # 5. Readiness Summary
            readiness = check_launch_readiness()
            readiness_summary = {
                "overall_status": readiness.get("overall_status"),
                "blockers_count": len(readiness.get("blockers", [])),
                "warnings_count": len(readiness.get("warnings", [])),
            }

            raw_data = {
                "site_config": site_data,
                "linkedin": linkedin_data,
                "mailboxes": mailboxes,
                "campaigns": campaigns,
                "readiness": readiness_summary,
            }

            # Canonical Masking
            metrics["data"] = mask_alert_payload(raw_data)
            metrics["success"] = True

        except Exception as e:
            errors.append(f"Config compile failed: {e}")

        # Output logic
        if output_path:
            if not errors:
                try:
                    # Create parent directory only when writing to file
                    parent = os.path.dirname(output_path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)

                    with open(output_path, "w", encoding="utf-8") as f:
                        json.dump(metrics["data"], f, indent=2)
                    metrics["exported_to_file"] = True
                except Exception as e:
                    errors.append(f"File write failed: {e}")

            if use_json:
                self.stdout.write(json.dumps(metrics, default=str, indent=2))
            else:
                if metrics["success"] and not errors:
                    self.stdout.write(f"Configuration successfully exported to {output_path}")
                else:
                    self.stderr.write(f"Export failed: {errors}")
        else:
            # Output directly to stdout
            if use_json:
                self.stdout.write(json.dumps(metrics["data"], default=str, indent=2))
            else:
                self.stdout.write(json.dumps(metrics["data"], default=str, indent=2))
