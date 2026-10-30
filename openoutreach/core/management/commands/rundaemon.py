import logging
import os
import sys

os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the HuntFlow daemon (onboard, validate, start task queue)."

    def add_arguments(self, parser):
        parser.add_argument("--handle", default=None, help="Django username to run daemon for")
        parser.add_argument("--profile-id", type=int, default=None, help="LinkedInProfile ID to run daemon for")
        parser.add_argument("--skip-onboarding", action="store_true", help="Skip CLI onboarding wizard check")
        parser.add_argument("--saas-worker", action="store_true", help="Run in multi-tenant SaaS worker mode")

    def handle(self, *args, **options):
        self._configure_logging(verbose=options.get("verbosity", 1) >= 2)
        self._ensure_db()
        
        is_saas = options.get("saas_worker") or options.get("skip_onboarding") or os.environ.get("HUNTFLOW_SAAS_WORKER", "").lower() == "true"
        if not is_saas:
            self._ensure_onboarded()

        session = self._create_session(options, is_saas=is_saas)

        from openoutreach.core.daemon import run_daemon
        run_daemon(session)

    # -- Steps ---------------------------------------------------------------

    def _configure_logging(self, verbose: bool = False):
        from openoutreach.core.logging import configure_logging, print_banner

        level = logging.DEBUG if verbose else logging.INFO
        configure_logging(level=level)
        print_banner()

    def _ensure_db(self):
        call_command("migrate", "--no-input")

        from openoutreach.core.management.setup_crm import setup_crm
        setup_crm()

    def _ensure_onboarded(self):
        from openoutreach.core.onboarding import apply, collect_from_wizard, missing_keys

        if not missing_keys():
            return

        if sys.stdin.isatty():
            apply(collect_from_wizard())
        else:
            missing = missing_keys()
            self.stderr.write(
                f"Onboarding incomplete and no TTY available.\n"
                f"Missing: {', '.join(sorted(missing))}\n"
                f"Run with an interactive terminal to complete onboarding."
            )
            sys.exit(1)

    def _create_session(self, options, is_saas: bool = False):
        from openoutreach.linkedin.browser.registry import get_first_active_profile, get_or_create_session
        from openoutreach.linkedin.models import LinkedInProfile
        from openoutreach.core.models import SiteConfig, Campaign
        from django.contrib.auth import get_user_model

        User = get_user_model()
        profile = None
        if options.get("profile_id"):
            profile = LinkedInProfile.objects.filter(pk=options["profile_id"]).select_related("user").first()
        elif options.get("handle"):
            profile = LinkedInProfile.objects.filter(user__username=options["handle"]).select_related("user").first()

        if profile is None:
            profile = get_first_active_profile()

        if profile is None:
            if is_saas:
                # In SaaS multi-tenant worker mode, create/use a base system profile or first profile
                profile = LinkedInProfile.objects.first()
                if profile is None:
                    admin_user = User.objects.filter(is_superuser=True).first() or User.objects.first()
                    if not admin_user:
                        admin_user = User.objects.create_user(username="system_worker")
                    profile = LinkedInProfile.objects.create(
                        user=admin_user,
                        linkedin_username="system_worker",
                        active=False,
                        session_status="needs_reauth",
                    )
            else:
                logger.error("No active LinkedIn profiles found.")
                sys.exit(1)

        session = get_or_create_session(profile)

        if not session.campaigns:
            # Fallback to any campaign in the workspace or system
            campaign = Campaign.objects.first()
            if campaign:
                session.campaign = campaign
            elif not is_saas:
                logger.error("No campaigns found for this user.")
                sys.exit(1)
        else:
            session.campaign = session.campaigns[0]

        return session

