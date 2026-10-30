from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import CandidateProfile, CandidateFactDraft

class Command(BaseCommand):
    help = "Review extracted fact drafts for a candidate."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--profile-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            profile = CandidateProfile.objects.get(
                pk=options["profile_id"],
                user__email=options["user_email"]
            )
        except CandidateProfile.DoesNotExist:
            raise CommandError("CandidateProfile not found.")

        drafts = CandidateFactDraft.objects.filter(
            extraction_record__profile=profile
        ).order_by("fact_type", "id")

        if not drafts.exists():
            self.stdout.write("No fact drafts found for this candidate.")
            return

        # Calculate a checksum of all draft IDs and contents for strict version verification
        import hashlib, json
        data_str = "".join([f"{d.id}:{json.dumps(d.value, sort_keys=True)}" for d in drafts])
        fact_set_checksum = hashlib.sha256(data_str.encode("utf-8")).hexdigest()

        self.stdout.write(f"Fact Set Checksum: {fact_set_checksum}")
        self.stdout.write("-" * 80)
        self.stdout.write(f"{'Draft ID':<10} | {'Fact Type':<12} | {'Category':<20} | {'Value / Description'}")
        self.stdout.write("-" * 80)
        for d in drafts:
            val_summary = str(d.value)
            if d.fact_type == "experience":
                val_summary = f"{d.value.get('title')} at {d.value.get('company')} ({d.value.get('start')} - {d.value.get('end')})"
            elif d.fact_type == "skill":
                val_summary = d.value.get("name")
            elif d.fact_type == "education":
                val_summary = f"{d.value.get('degree')} from {d.value.get('school')} ({d.value.get('year')})"
            elif d.fact_type == "certification":
                val_summary = f"{d.value.get('name')} by {d.value.get('issuer')} ({d.value.get('year')})"
            
            self.stdout.write(f"{d.id:<10} | {d.fact_type:<12} | {d.category[:20]:<20} | {val_summary}")
        self.stdout.write("-" * 80)
