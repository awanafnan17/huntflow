from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile, ActionLog, ConnectionAttempt
from openoutreach.linkedin.browser.session import AccountSession
from openoutreach.linkedin.browser.connect_with_note import send_connection_request_with_note
import hashlib

class Command(BaseCommand):
    help = "Pilot LinkedIn connection requests with safety checks and dry-run controls."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Perform validation checks but do not send request.")
        parser.add_argument("--execute", action="store_true", help="Execute the connection request.")
        parser.add_argument("--profile-id", type=int, help="ID of the LinkedInProfile to use.")
        parser.add_argument("--user-email", help="Email of the Django user to resolve LinkedInProfile.")
        parser.add_argument("--expected-recipient-id", required=True, help="Expected recipient public identifier.")
        parser.add_argument("--expected-content-checksum", required=True, help="SHA256 checksum of the personalized note.")
        parser.add_argument("--idempotency-key", required=True, help="Unique key to prevent duplicate runs.")
        parser.add_argument("--note-text", required=True, help="The personalized note text.")

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
        
        # Calculate checksum on the note
        note_text = options["note_text"]
        actual_checksum = hashlib.sha256(note_text.encode("utf-8")).hexdigest()
        if actual_checksum != options["expected_content_checksum"]:
            raise CommandError(f"Note checksum mismatch! Expected: {options['expected_content_checksum']}, Actual: {actual_checksum}")

        self.stdout.write(f"Idempotency Key: {options['idempotency_key']}")
        self.stdout.write(f"LinkedIn Username: {profile.linkedin_username}")
        self.stdout.write(f"Session Status: {profile.session_status}")
        self.stdout.write(f"Target Recipient: {expected_recipient}")
        self.stdout.write(f"Note (length={len(note_text)}): {note_text}")

        # Check safety holds & verification
        from openoutreach.career.models import CandidateProfile
        from openoutreach.career.service import check_safety_holds_and_verification
        cand_profile = CandidateProfile.objects.filter(user=profile.user).first()
        check_safety_holds_and_verification(profile=cand_profile, is_dry_run=options["dry_run"])

        # Check idempotency key against ledger
        from django.utils import timezone
        from openoutreach.linkedin.models import ConnectionAttempt, ConnectionChildAttempt
        key = options["idempotency_key"]
        parent, parent_created = ConnectionAttempt.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "linkedin_profile": profile,
                "recipient_public_id": expected_recipient,
                "status": "prepared",
                "note_text": note_text
            }
        )

        if not parent_created and parent.child_attempts.exists():
            raise CommandError(f"Idempotency key {key} already used. Return existing status: {parent.status}")

        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS("Dry-run validation checks passed! Ready for connection pilot."))
            return

        # Create child attempt ledger row
        seq = parent.child_attempts.count() + 1
        child = ConnectionChildAttempt.objects.create(
            parent=parent,
            sequence=seq,
            timestamp=timezone.now(),
            started_at=timezone.now(),
            recipient=expected_recipient,
            attempted_url=f"https://www.linkedin.com/in/{expected_recipient}/",
            outcome="attempting"
        )

        # Execute - update status to attempting before initiating any network action
        parent.status = "attempting"
        parent.save()

        self.stdout.write("Initializing browser session...")
        session = AccountSession(profile)
        session.ensure_browser()

        error_to_raise = None
        try:
            url = f"https://www.linkedin.com/in/{expected_recipient}/"
            self.stdout.write(f"Navigating to recipient profile page: {url}")
            session.page.goto(url)
            session.page.wait_for_timeout(5000)
            self.stdout.write(f"Sending connection request with note to {expected_recipient}...")
            recipient_profile = {"public_identifier": expected_recipient}
            res = send_connection_request_with_note(
                session=session,
                profile=recipient_profile,
                note=note_text
            )
        except Exception as e:
            error_to_raise = e
        finally:
            try:
                if session.page:
                    session.page.close()
                if session.context:
                    session.context.close()
                if session.browser:
                    session.browser.close()
            except Exception:
                pass

        if error_to_raise:
            from openoutreach.linkedin.models import ConnectionAttempt, ConnectionChildAttempt
            p = ConnectionAttempt.objects.get(id=parent.id)
            p.status = "failed"
            p.error_message = str(error_to_raise)
            p.save()
            c = ConnectionChildAttempt.objects.get(id=child.id)
            c.outcome = "failed"
            c.finished_at = timezone.now()
            c.exception_type = type(error_to_raise).__name__
            c.error_classification = "top_card_not_found" if "top_card_not_found" in str(error_to_raise) else ""
            c.save()
            raise error_to_raise
        else:
            from openoutreach.core.models import Campaign
            camp = Campaign.objects.first()
            profile.record_action(
                ActionLog.ActionType.CONNECT,
                campaign=camp,
                metadata={
                    "lead_public_id": expected_recipient,
                    "note_length": len(note_text),
                    "note_was_sent": True,
                    "idempotency_key": options["idempotency_key"]
                }
            )
            parent.status = "externally_confirmed"
            parent.save()
            child.outcome = "externally_confirmed"
            child.finished_at = timezone.now()
            child.externally_sent = True
            child.save()
            if session.playwright:
                session.playwright.stop()
