import csv
import io
from typing import Optional, Dict, Any, List
from django.db import transaction
from django.core.exceptions import ValidationError

from openoutreach.core.services.entitlements import consume_entitlement
from openoutreach.core.services.audit_service import record_audit_event
from openoutreach.crm.models.deal import DealState
from openoutreach.crm.models.lead import (
    Lead,
    SuppressionRecord,
    TargetImport,
    TargetImportMapping,
    TargetImportRow,
)
from openoutreach.crm.services.deal_service import create_deal


def upload_target_import(
    workspace,
    name: str,
    file_name: str,
    csv_content: str,
    campaign=None,
    user=None,
) -> TargetImport:
    if campaign and campaign.workspace != workspace:
        raise ValidationError("Import campaign workspace must match target import workspace.")

    reader = csv.DictReader(io.StringIO(csv_content))
    rows = list(reader)

    target_import = TargetImport.objects.create(
        workspace=workspace,
        campaign=campaign,
        name=name,
        source_type="csv",
        file_name=file_name,
        total_rows=len(rows),
        status="uploaded",
        created_by=user,
    )

    for idx, row in enumerate(rows, start=1):
        TargetImportRow.objects.create(
            target_import=target_import,
            row_index=idx,
            raw_data=dict(row),
            validation_status="pending",
        )

    return target_import


def set_import_mapping(target_import: TargetImport, column_mapping: Dict[str, str]) -> TargetImportMapping:
    """Step 2: Save column mapping."""
    mapping, _ = TargetImportMapping.objects.update_or_create(
        target_import=target_import,
        defaults={"column_mapping": column_mapping},
    )
    target_import.status = "mapped"
    target_import.save(update_fields=["status"])
    return mapping


def preview_and_validate_import(target_import: TargetImport) -> Dict[str, Any]:
    """Step 3: Validate and preview import.
    PERFORMS ZERO OPERATIONAL WRITES to Lead/Deal tables.
    """
    try:
        mapping_obj = target_import.mapping
        mapping = mapping_obj.column_mapping
    except TargetImportMapping.DoesNotExist:
        raise ValidationError("Import column mapping must be configured before previewing.")

    ws = target_import.workspace
    existing_linkedin_urls = set(
        Lead.objects.for_workspace(ws).exclude(linkedin_url="").values_list("linkedin_url", flat=True)
    )
    existing_emails = set(
        Lead.objects.for_workspace(ws).exclude(api_email="").values_list("api_email", flat=True)
    )
    suppressed_urls = set(
        SuppressionRecord.objects.for_workspace(ws).values_list("linkedin_url", flat=True)
    )

    valid_count = 0
    duplicate_count = 0
    suppressed_count = 0
    invalid_count = 0

    seen_urls_in_batch = set()
    seen_emails_in_batch = set()

    for row in target_import.rows.all():
        raw = row.raw_data
        normalized = {}
        for db_field, csv_header in mapping.items():
            if csv_header in raw and raw[csv_header]:
                normalized[db_field] = raw[csv_header].strip()

        li_url = normalized.get("linkedin_url", "")
        email = normalized.get("email", "") or normalized.get("api_email", "")

        errors = []
        status = "valid"

        if not li_url and not email:
            errors.append("Row must provide either a LinkedIn URL or an email address.")
            status = "invalid"

        if li_url and li_url in suppressed_urls:
            errors.append(f"LinkedIn URL '{li_url}' is suppressed.")
            status = "suppressed"
            record_audit_event(
                workspace=ws,
                event_type="target_suppressed",
                actor=target_import.created_by,
                action_key=f"supp_{target_import.id}_{row.id}",
                metadata={"linkedin_url": li_url},
            )
        elif li_url and (li_url in existing_linkedin_urls or li_url in seen_urls_in_batch):
            errors.append(f"LinkedIn URL '{li_url}' is a duplicate.")
            status = "duplicate"
        elif email and (email in existing_emails or email in seen_emails_in_batch):
            errors.append(f"Email '{email}' is a duplicate.")
            status = "duplicate"

        if status == "valid":
            if li_url:
                seen_urls_in_batch.add(li_url)
            if email:
                seen_emails_in_batch.add(email)
            valid_count += 1
        elif status == "duplicate":
            duplicate_count += 1
        elif status == "suppressed":
            suppressed_count += 1
        else:
            invalid_count += 1

        row.normalized_data = normalized
        row.validation_status = status
        row.error_message = "; ".join(errors)
        row.save(update_fields=["normalized_data", "validation_status", "error_message"])

    target_import.status = "previewed"
    target_import.save(update_fields=["status"])

    record_audit_event(
        workspace=ws,
        event_type="target_import_previewed",
        actor=target_import.created_by,
        target=target_import,
        action_key=f"prev_{target_import.id}",
        metadata={"valid": valid_count, "invalid": invalid_count, "duplicate": duplicate_count},
    )

    return {
        "total": target_import.total_rows,
        "valid": valid_count,
        "duplicate": duplicate_count,
        "suppressed": suppressed_count,
        "invalid": invalid_count,
    }


def confirm_and_execute_import(target_import: TargetImport) -> TargetImport:
    """Step 4: Execute import transactionally."""
    if target_import.status not in ["previewed", "validated", "confirmed"]:
        preview_and_validate_import(target_import)

    ws = target_import.workspace
    valid_rows = list(target_import.rows.filter(validation_status="valid"))
    valid_count = len(valid_rows)

    consume_entitlement(
        workspace=ws,
        metric="monthly_leads",
        quantity=valid_count,
        action_key=f"ent_imp_{target_import.id}",
    )

    target_import.status = "confirmed"
    target_import.save(update_fields=["status"])

    campaign = target_import.campaign

    imported_count = 0
    failed_count = 0

    with transaction.atomic():
        for row in valid_rows:
            data = row.normalized_data
            li_url = data.get("linkedin_url", "")
            pub_id = data.get("public_identifier", "") or (li_url.rstrip("/").split("/")[-1] if li_url else "") or data.get("email", "")
            try:
                with transaction.atomic():
                    lead = Lead.objects.create(
                        workspace=ws,
                        public_identifier=pub_id,
                        linkedin_url=li_url,
                        first_name=data.get("first_name", ""),
                        last_name=data.get("last_name", ""),
                        headline=data.get("headline", ""),
                        current_title=data.get("current_title", ""),
                        company=data.get("company", ""),
                        location=data.get("location", ""),
                        industry=data.get("industry", ""),
                        api_email=data.get("email", ""),
                    )
                    row.created_lead = lead
                    record_audit_event(
                        workspace=ws,
                        event_type="target_created",
                        actor=target_import.created_by,
                        target=lead,
                        action_key=f"lead_create_{lead.id}",
                        metadata={"public_identifier": pub_id},
                    )

                    if campaign:
                        deal = create_deal(
                            campaign=campaign,
                            lead=lead,
                            state=data.get("deal_state", DealState.QUALIFIED),
                        )
                        row.created_deal = deal
                        record_audit_event(
                            workspace=ws,
                            event_type="deal_created",
                            actor=target_import.created_by,
                            target=deal,
                            action_key=f"deal_create_{deal.id}",
                            metadata={"campaign_id": campaign.id},
                        )

                    row.validation_status = "imported"
                    row.save(update_fields=["created_lead", "created_deal", "validation_status"])
                    imported_count += 1
            except Exception as exc:
                row.validation_status = "invalid"
                row.error_message = f"Execution error: {exc}"
                row.save(update_fields=["validation_status", "error_message"])
                failed_count += 1

        failed_count = target_import.rows.exclude(validation_status="imported").count()
        target_import.imported_rows = imported_count
        target_import.failed_rows = failed_count
        target_import.status = "imported" if failed_count == 0 else "partially_imported"
        target_import.save(update_fields=["imported_rows", "failed_rows", "status"])

        event_type = "target_import_confirmed" if failed_count == 0 else "target_import_failed"
        record_audit_event(
            workspace=ws,
            event_type=event_type,
            actor=target_import.created_by,
            target=target_import,
            action_key=f"imp_confirm_{target_import.id}",
            metadata={"imported": imported_count, "failed": failed_count},
        )

    return target_import
