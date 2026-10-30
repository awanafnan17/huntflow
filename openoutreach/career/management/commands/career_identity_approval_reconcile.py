import sys
from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, CandidateProfileApproval
from openoutreach.career.service import reconcile_corrected_identity_approval_state

class Command(BaseCommand):
    help = "Reconcile corrected identity approval state and perform preflight checks."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--historical-version-id", type=int, required=True)
        parser.add_argument("--expected-corrected-version-id", type=int, required=False)

    def handle(self, *args, **options):
        # Determine database context
        from django.db import connection as django_connection
        db_path = django_connection.settings_dict.get("NAME", "")
        is_test_db = str(db_path).startswith("test_")
        
        self.stdout.write("=" * 60)
        if is_test_db:
            self.stdout.write(f"DATABASE CONTEXT: TEST DATABASE ({db_path})")
        elif "db.sqlite3" in str(db_path) or "Downloads" in str(db_path):
            self.stdout.write(f"DATABASE CONTEXT: LOCAL DEVELOPMENT DATABASE ({db_path})")
        else:
            self.stdout.write(f"DATABASE CONTEXT: NON-TEST DATABASE ({db_path})")
        self.stdout.write("=" * 60 + "\n")

        # 1. Fetch CandidateProfile
        profile = CandidateProfile.objects.filter(user__email=options["user_email"]).first()
        if not profile:
            raise CommandError(f"CandidateProfile not found for email: {options['user_email']}")

        # Capture database snapshot BEFORE reconciliation run
        from openoutreach.career.service import capture_canonical_db_snapshot, compare_db_snapshots
        snap_before = capture_canonical_db_snapshot(profile)

        # 2. Run Reconciliation
        res = reconcile_corrected_identity_approval_state(
            profile=profile,
            expected_historical_version_id=options["historical_version_id"],
            expected_corrected_version_id=options.get("expected_corrected_version_id")
        )

        # Capture database snapshot AFTER reconciliation run
        snap_after = capture_canonical_db_snapshot(profile)
        snapshots_equal = compare_db_snapshots(snap_before, snap_after)

        # 3. Print Report
        self.stdout.write("VERSION ROLES")
        for vid, role in res["version_roles"].items():
            ver = CandidateProfileVersion.objects.get(pk=vid)
            self.stdout.write(f"  Version ID {vid} (number {ver.version_number}): {role} (Name: {ver.full_name}, State: {ver.state})")
        self.stdout.write("")

        self.stdout.write("RECONCILIATION QUESTIONS")
        for q, a in res["questions"].items():
            self.stdout.write(f"  {q}\n  -> {a}")
        self.stdout.write("")

        # Explicitly print Source Evidence vs Corrected Lineage Status
        source_evidence_status = "PRESENT" if res["source_provenance"]["extracted_name_found"] else "ABSENT"
        corrected_lineage_status = "ABSENT"
        versions = list(CandidateProfileVersion.objects.filter(profile=profile))
        corrected_versions = [v for v in versions if v.id != options["historical_version_id"] and v.id not in [v2.id for v2 in versions if "idwsdsjtasdkk" in (v2.full_name or "")]]
        if corrected_versions:
            corrected_lineage_status = "PRESENT"
            
        review_provenance_status = "NOT ESTABLISHED"
        if res["questions"]["12. Was Version 5 reviewed by the hardened read-only review service before approval?"] == "ESTABLISHED":
            review_provenance_status = "ESTABLISHED"

        self.stdout.write("PROVENANCE AND LINEAGE STATUS")
        self.stdout.write(f"  SOURCE IDENTITY EVIDENCE: {source_evidence_status}")
        self.stdout.write(f"  CORRECTED VERSION LINEAGE: {corrected_lineage_status}")
        self.stdout.write(f"  REVIEW PROVENANCE: {review_provenance_status}")
        self.stdout.write("")

        self.stdout.write("SOURCE PROVENANCE FINDINGS")
        self.stdout.write(f"  Source documents found: {len(res['source_provenance']['documents'])}")
        for d in res['source_provenance']['documents']:
            self.stdout.write(f"    - Document ID {d['id']}: {d['name']}")
        self.stdout.write(f"  Document versions count: {len(res['source_provenance']['document_versions'])}")
        self.stdout.write(f"  Extraction records count: {len(res['source_provenance']['extractions'])}")
        self.stdout.write(f"  Name 'Alex Morgan' found in extraction source text: {res['source_provenance']['extracted_name_found']}")
        self.stdout.write("")

        self.stdout.write("VERSION 3 IMMUTABILITY")
        self.stdout.write(f"  Consistency status: {res['v3_immutability']['consistency']}")
        self.stdout.write(f"  Stored checksum consistent: {res['v3_immutability']['checksum_consistent']}")
        self.stdout.write(f"  Canonical current-row snapshot unchanged by reconciliation: {res['v3_immutability']['snapshot_unchanged']}")
        self.stdout.write(f"  Trusted prior byte baseline available: {res['v3_immutability']['prior_byte_baseline_available']}")
        self.stdout.write("")

        self.stdout.write("INCIDENT HOLD STATUS")
        self.stdout.write(f"  IncidentHold Active: {res['incident_hold_status']['active']}")
        for h in res['incident_hold_status']['holds']:
            self.stdout.write(f"    - Hold ID {h['id']}: {h['reason']}")
        self.stdout.write(f"  Targeted Quarantine count: {len(res['incident_hold_status']['targets'])}")
        for t in res['incident_hold_status']['targets']:
            self.stdout.write(f"    - Target model: {t['model']}, ID: {t['object_id']}")
        self.stdout.write("")

        # Approval Mapping
        self.stdout.write("APPROVAL MAPPING")
        approvals = list(CandidateProfileApproval.objects.filter(profile_version__profile=profile))
        if approvals:
            for app in approvals:
                self.stdout.write(f"  Approval ID {app.id} -> Version ID {app.profile_version.id} (approved_by: {app.approved_by.username}, reason: {app.approval_reason or 'None'}, key: {app.idempotency_key})")
        else:
            self.stdout.write("  No approval records found.")
        self.stdout.write("")

        # Hashing and Canonicalization Audit Output
        self.stdout.write("CANONICAL MODEL SNAPSHOT INTEGRITY REPORT")
        self.stdout.write("  Deterministic ordering: elements sorted by primary key ID")
        self.stdout.write("  Datetime serialization: converted to ISO 8601 format")
        self.stdout.write("  JSON field canonicalization: json.dumps used with sorted keys")
        self.stdout.write("  Excluded fields: None (all model fields included to guarantee complete byte-level integrity)")
        for model in snap_before:
            before_digest = snap_before[model]["digest"]
            after_digest = snap_after[model]["digest"]
            unchanged = before_digest == after_digest
            self.stdout.write(f"    - {model} before digest: {before_digest}")
            self.stdout.write(f"    - {model} after digest:  {after_digest}")
            self.stdout.write(f"      Unchanged: {unchanged}")
        self.stdout.write("")

        self.stdout.write("MUTATION STATUS")
        self.stdout.write(f"  {res['mutation_status']}")
        if snapshots_equal:
            self.stdout.write("  CANONICAL DATABASE SNAPSHOT UNCHANGED: PASS\n")
        else:
            self.stdout.write("  CANONICAL DATABASE SNAPSHOT UNCHANGED: FAIL\n")

        self.stdout.write("PREFLIGHT STATUS")
        self.stdout.write(f"  {res['preflight_status']}")
        if res["preflight_reasons"]:
            self.stdout.write("  Reasons:")
            for r in res["preflight_reasons"]:
                self.stdout.write(f"    - {r}")
        self.stdout.write("")

        # 4. Exit Code Handling
        if res["preflight_status"] == "IDENTITY VERIFICATION PREFLIGHT: BLOCKED":
            self.stdout.write("Safety Check Result: Non-zero exit is a safety control block, not a crash.")
            sys.exit(1)
        
        if "unexpected" in res["version_roles"].values():
            self.stdout.write("Safety Check Result: Non-zero exit due to unexpected version roles.")
            sys.exit(2)
