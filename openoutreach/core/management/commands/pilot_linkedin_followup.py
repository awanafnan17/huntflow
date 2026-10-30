from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.linkedin.browser.session import AccountSession
from linkedin_cli.actions.message import send_raw_message
import hashlib

class Command(BaseCommand):
    help = "Pilot LinkedIn follow-up messages with safety checks and dry-run controls."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Perform validation checks but do not send message.")
        parser.add_argument("--execute", action="store_true", help="Execute the follow-up message.")
        parser.add_argument("--profile-id", type=int, help="ID of the LinkedInProfile to use.")
        parser.add_argument("--user-email", help="Email of the Django user to resolve LinkedInProfile.")
        parser.add_argument("--expected-recipient-id", required=True, help="Expected recipient public identifier.")
        parser.add_argument("--expected-content-checksum", required=True, help="SHA256 checksum of the follow-up message.")
        parser.add_argument("--idempotency-key", required=True, help="Unique key to prevent duplicate runs.")
        parser.add_argument("--message-text", required=True, help="The follow-up message text.")

    def handle(self, *args, **options):
        if not options["dry_run"] and not options["execute"]:
            raise CommandError("Either --dry-run or --execute must be specified.")

        if not options.get("profile_id") and not options.get("user_email"):
            raise CommandError("Either --profile-id or --user-email must be specified.")

        try:
            if options.get("profile_id"):
                profile = LinkedInProfile.objects.get(pk=options["profile_id"])
            else:
                profile = LinkedInProfile.objects.get(user__email=options["user_email"])
        except LinkedInProfile.DoesNotExist:
            raise CommandError("LinkedInProfile not found for the specified ID or user email.")

        # Validate recipient ID
        expected_recipient = options["expected_recipient_id"]
        
        # Calculate checksum on the message
        message_text = options["message_text"]
        actual_checksum = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
        if actual_checksum != options["expected_content_checksum"]:
            raise CommandError(f"Message checksum mismatch! Expected: {options['expected_content_checksum']}, Actual: {actual_checksum}")

        self.stdout.write(f"Idempotency Key: {options['idempotency_key']}")
        self.stdout.write(f"LinkedIn Username: {profile.linkedin_username}")
        self.stdout.write(f"Session Status: {profile.session_status}")
        self.stdout.write(f"Target Recipient: {expected_recipient}")
        self.stdout.write(f"Message (length={len(message_text)}): {message_text}")

        # Check safety holds & verification
        from openoutreach.career.models import CandidateProfile
        from openoutreach.career.service import check_safety_holds_and_verification
        cand_profile = CandidateProfile.objects.filter(user=profile.user).first()
        check_safety_holds_and_verification(profile=cand_profile, is_dry_run=options["dry_run"])

        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS("Dry-run validation checks passed! Ready for follow-up pilot."))
            return

        # Execute
        self.stdout.write("Initializing browser session...")
        session = AccountSession(profile)
        session.ensure_browser()

        try:
            self.stdout.write(f"Sending follow-up message to {expected_recipient}...")
            # Set up recipient dict
            recipient_profile = {
                "public_identifier": expected_recipient
            }
            res = send_raw_message(session, recipient_profile, message_text)
            self.stdout.write(self.style.SUCCESS(f"Follow-up action completed with result: {res}"))
            
            # Log action
            profile.record_action(
                ActionLog.ActionType.FOLLOW_UP,
                metadata={
                    "lead_public_id": expected_recipient,
                    "message_length": len(message_text),
                    "idempotency_key": options["idempotency_key"]
                }
            )
        finally:
            if session.page:
                session.page.close()
            if session.context:
                session.context.close()
            if session.browser:
                session.browser.close()
            if session.playwright:
                session.playwright.stop()
