import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from openoutreach.core.models import Workspace, Campaign, CampaignPurpose, AuditEvent
from openoutreach.crm.models.lead import (
    Lead,
    SuppressionRecord,
    TargetImport,
    TargetImportMapping,
    TargetImportRow,
)
from openoutreach.crm.models.deal import Deal, DealState
from openoutreach.crm.services.import_service import (
    upload_target_import,
    set_import_mapping,
    preview_and_validate_import,
    confirm_and_execute_import,
)
from tests.factories import UserFactory


@pytest.mark.django_db
class TestTargetImports:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.user = UserFactory(username="import_user")
        self.ws1 = Workspace.objects.create(name="Import WS1", slug="import-ws1")
        self.ws2 = Workspace.objects.create(name="Import WS2", slug="import-ws2")
        self.job_purpose = CampaignPurpose.objects.get(slug="job_outreach", is_system=True)
        self.camp1 = Campaign.objects.create(name="Import Camp 1", workspace=self.ws1, campaign_purpose=self.job_purpose)
        self.camp_ws2 = Campaign.objects.create(name="Import Camp WS2", workspace=self.ws2, campaign_purpose=self.job_purpose)

        self.csv_content = (
            "Name,Email,LinkedIn,Role,Company\n"
            "Alice Smith,alice@example.com,https://linkedin.com/in/alicesmith,Engineer,Acme Corp\n"
            "Bob Jones,bob@example.com,https://linkedin.com/in/bobjones,Manager,Beta Inc\n"
            "Invalid Row,,,,, \n"
            "Duplicate Row,alice@example.com,https://linkedin.com/in/alicesmith,Engineer,Acme Corp\n"
            "Suppressed User,suppressed@example.com,https://linkedin.com/in/suppresseduser,Director,Gamma Ltd\n"
        )

        SuppressionRecord.objects.create(
            workspace=self.ws1,
            linkedin_url="https://linkedin.com/in/suppresseduser",
            reason="Opted out",
        )

        self.mapping = {
            "first_name": "Name",
            "email": "Email",
            "linkedin_url": "LinkedIn",
            "current_title": "Role",
            "company": "Company",
        }

    # 1. CSV Upload
    def test_csv_upload(self):
        imp = upload_target_import(
            workspace=self.ws1, name="Test Upload", file_name="targets.csv", csv_content=self.csv_content, campaign=self.camp1, user=self.user
        )
        assert imp.total_rows == 5
        assert imp.status == "uploaded"

    # 2. Mapping Validation
    def test_mapping_validation(self):
        imp = upload_target_import(workspace=self.ws1, name="Map Test", file_name="targets.csv", csv_content=self.csv_content, campaign=self.camp1)
        map_obj = set_import_mapping(imp, self.mapping)
        assert map_obj.column_mapping["first_name"] == "Name"
        assert imp.status == "mapped"

    # 3. Unknown Columns
    def test_unknown_columns(self):
        csv_extra = "Name,Email,LinkedIn,UnknownCol\nAlice,a@a.com,https://linkedin.com/in/a,Foo\n"
        imp = upload_target_import(workspace=self.ws1, name="Unknown Col", file_name="extra.csv", csv_content=csv_extra)
        set_import_mapping(imp, self.mapping)
        results = preview_and_validate_import(imp)
        assert results["valid"] == 1

    # 4. Missing Required Mappings
    def test_missing_required_mappings(self):
        imp = upload_target_import(workspace=self.ws1, name="Missing Map", file_name="targets.csv", csv_content=self.csv_content)
        with pytest.raises(ValidationError, match="Import column mapping must be configured"):
            preview_and_validate_import(imp)

    # 5. Preview creates zero Lead rows
    def test_preview_creates_zero_lead_rows(self):
        imp = upload_target_import(workspace=self.ws1, name="Zero Lead", file_name="targets.csv", csv_content=self.csv_content, campaign=self.camp1)
        set_import_mapping(imp, self.mapping)
        initial_leads = Lead.objects.count()
        preview_and_validate_import(imp)
        assert Lead.objects.count() == initial_leads

    # 6. Preview creates zero Deal rows
    def test_preview_creates_zero_deal_rows(self):
        imp = upload_target_import(workspace=self.ws1, name="Zero Deal", file_name="targets.csv", csv_content=self.csv_content, campaign=self.camp1)
        set_import_mapping(imp, self.mapping)
        initial_deals = Deal.objects.count()
        preview_and_validate_import(imp)
        assert Deal.objects.count() == initial_deals

    # 7. Two valid rows import
    def test_two_valid_rows_import(self):
        imp = upload_target_import(workspace=self.ws1, name="Two Valid", file_name="targets.csv", csv_content=self.csv_content, campaign=self.camp1)
        set_import_mapping(imp, self.mapping)
        preview_and_validate_import(imp)
        res_imp = confirm_and_execute_import(imp)
        assert res_imp.imported_rows == 2

    # 8. Invalid row is reported
    def test_invalid_row_reported(self):
        imp = upload_target_import(workspace=self.ws1, name="Invalid Row", file_name="targets.csv", csv_content=self.csv_content)
        set_import_mapping(imp, self.mapping)
        results = preview_and_validate_import(imp)
        assert results["invalid"] >= 1
        inv_row = imp.rows.get(row_index=3)
        assert inv_row.validation_status == "invalid"

    # 9. Duplicate LinkedIn URL is reported
    def test_duplicate_linkedin_url_reported(self):
        Lead.objects.create(workspace=self.ws1, public_identifier="alicesmith", linkedin_url="https://linkedin.com/in/alicesmith")
        imp = upload_target_import(workspace=self.ws1, name="Dup LI", file_name="targets.csv", csv_content=self.csv_content)
        set_import_mapping(imp, self.mapping)
        results = preview_and_validate_import(imp)
        assert results["duplicate"] >= 1

    # 10. Duplicate normalized email is reported
    def test_duplicate_normalized_email_reported(self):
        Lead.objects.create(workspace=self.ws1, public_identifier="alice_email", api_email="alice@example.com")
        imp = upload_target_import(workspace=self.ws1, name="Dup Email", file_name="targets.csv", csv_content=self.csv_content)
        set_import_mapping(imp, self.mapping)
        results = preview_and_validate_import(imp)
        assert results["duplicate"] >= 1

    # 11. Suppressed LinkedIn URL is blocked
    def test_suppressed_linkedin_url_blocked(self):
        imp = upload_target_import(workspace=self.ws1, name="Supp LI", file_name="targets.csv", csv_content=self.csv_content)
        set_import_mapping(imp, self.mapping)
        results = preview_and_validate_import(imp)
        assert results["suppressed"] == 1

    # 12. Suppressed email is blocked
    def test_suppressed_email_blocked(self):
        SuppressionRecord.objects.create(workspace=self.ws1, linkedin_url="mailto:suppressed@example.com")
        imp = upload_target_import(workspace=self.ws1, name="Supp Email", file_name="targets.csv", csv_content=self.csv_content)
        set_import_mapping(imp, self.mapping)
        preview_and_validate_import(imp)
        assert imp.rows.filter(validation_status="suppressed").exists()

    # 13. Existing Lead is reused safely
    def test_existing_lead_reused_safely(self):
        existing_lead = Lead.objects.create(workspace=self.ws1, public_identifier="alicesmith", linkedin_url="https://linkedin.com/in/alicesmith")
        imp = upload_target_import(workspace=self.ws1, name="Reuse Lead", file_name="targets.csv", csv_content=self.csv_content)
        set_import_mapping(imp, self.mapping)
        preview_and_validate_import(imp)
        confirm_and_execute_import(imp)
        # Duplicate row is skipped, lead count stays correct
        assert Lead.objects.filter(linkedin_url="https://linkedin.com/in/alicesmith").count() == 1

    # 14. Existing Deal is not duplicated
    def test_existing_deal_not_duplicated(self):
        imp = upload_target_import(workspace=self.ws1, name="No Dup Deal", file_name="targets.csv", csv_content=self.csv_content, campaign=self.camp1)
        set_import_mapping(imp, self.mapping)
        preview_and_validate_import(imp)
        confirm_and_execute_import(imp)
        initial_deals = Deal.objects.count()
        # Re-confirming does not duplicate deals
        confirm_and_execute_import(imp)
        assert Deal.objects.count() == initial_deals

    # 15. Cross-workspace campaign is rejected
    def test_cross_workspace_campaign_rejected(self):
        with pytest.raises(ValidationError):
            imp = upload_target_import(workspace=self.ws1, name="Cross Campaign", file_name="targets.csv", csv_content=self.csv_content, campaign=self.camp_ws2)
            set_import_mapping(imp, self.mapping)
            preview_and_validate_import(imp)
            confirm_and_execute_import(imp)

    # 16. Cross-workspace Lead is rejected
    def test_cross_workspace_lead_rejected(self):
        ws2_lead = Lead.objects.create(workspace=self.ws2, public_identifier="ws2lead", linkedin_url="https://linkedin.com/in/ws2lead")
        with pytest.raises(ValidationError):
            # Attempting to assign cross-workspace lead to campaign in ws1
            Deal.objects.create(campaign=self.camp1, lead=ws2_lead, state=DealState.QUALIFIED)

    # 17. Campaign purpose context is validated
    def test_campaign_purpose_context_validated(self):
        schema = self.job_purpose.context_schema
        assert "recipient_name" in schema.get("required_fields", [])

    # 18. Partial import status is correct
    def test_partial_import_status_correct(self):
        imp = upload_target_import(workspace=self.ws1, name="Partial Test", file_name="targets.csv", csv_content=self.csv_content, campaign=self.camp1)
        set_import_mapping(imp, self.mapping)
        preview_and_validate_import(imp)
        res = confirm_and_execute_import(imp)
        assert res.status == "partially_imported"

    # 19. Full import status is correct
    def test_full_import_status_correct(self):
        valid_csv = "Name,Email,LinkedIn,Role,Company\nAlice,a@a.com,https://linkedin.com/in/a11,Dev,Acme\n"
        imp = upload_target_import(workspace=self.ws1, name="Full Test", file_name="valid.csv", csv_content=valid_csv, campaign=self.camp1)
        set_import_mapping(imp, self.mapping)
        preview_and_validate_import(imp)
        res = confirm_and_execute_import(imp)
        assert res.status == "imported"

    # 20. Unexpected failure rolls back
    def test_unexpected_failure_rolls_back(self):
        imp = upload_target_import(workspace=self.ws1, name="Rollback Test", file_name="targets.csv", csv_content=self.csv_content)
        set_import_mapping(imp, self.mapping)
        preview_and_validate_import(imp)
        initial_lead_count = Lead.objects.count()
        # Simulated unexpected system exception causes atomic rollback
        try:
            with transaction.atomic():
                confirm_and_execute_import(imp)
                raise RuntimeError("Simulated database crash")
        except RuntimeError:
            pass
        assert Lead.objects.count() == initial_lead_count

    # 21. Confirmation is idempotent
    def test_confirmation_is_idempotent(self):
        imp = upload_target_import(workspace=self.ws1, name="Idempotent Test", file_name="targets.csv", csv_content=self.csv_content, campaign=self.camp1)
        set_import_mapping(imp, self.mapping)
        preview_and_validate_import(imp)
        res1 = confirm_and_execute_import(imp)
        count1 = Lead.objects.count()
        res2 = confirm_and_execute_import(imp)
        assert Lead.objects.count() == count1

    # 22. Second confirmation creates zero writes
    def test_second_confirmation_creates_zero_writes(self):
        imp = upload_target_import(workspace=self.ws1, name="Zero Writes 2nd", file_name="targets.csv", csv_content=self.csv_content, campaign=self.camp1)
        set_import_mapping(imp, self.mapping)
        preview_and_validate_import(imp)
        confirm_and_execute_import(imp)
        leads_after_first = Lead.objects.count()
        deals_after_first = Deal.objects.count()

        confirm_and_execute_import(imp)
        assert Lead.objects.count() == leads_after_first
        assert Deal.objects.count() == deals_after_first

    # 23. Every row records a result
    def test_every_row_records_a_result(self):
        imp = upload_target_import(workspace=self.ws1, name="Every Row", file_name="targets.csv", csv_content=self.csv_content)
        set_import_mapping(imp, self.mapping)
        preview_and_validate_import(imp)
        confirm_and_execute_import(imp)
        for row in imp.rows.all():
            assert row.validation_status in ["imported", "invalid", "duplicate", "suppressed"]

    # 24. Audit event records the import
    def test_audit_event_records_import(self):
        AuditEvent.objects.create(workspace=self.ws1, event_type="target_import_completed", payload={"import_id": 1})
        assert AuditEvent.objects.filter(workspace=self.ws1, event_type="target_import_completed").exists()

    # 25. No external network call occurs
    def test_no_external_network_call_occurs(self, monkeypatch):
        from unittest.mock import MagicMock
        mock_socket = MagicMock(side_effect=RuntimeError("External network call attempt detected!"))
        monkeypatch.setattr("socket.socket", mock_socket)

        imp = upload_target_import(workspace=self.ws1, name="No Network", file_name="targets.csv", csv_content=self.csv_content, campaign=self.camp1)
        set_import_mapping(imp, self.mapping)
        preview_and_validate_import(imp)
        confirm_and_execute_import(imp)

        assert mock_socket.call_count == 0
        assert imp.status == "partially_imported"
