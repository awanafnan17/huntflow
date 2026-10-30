from django.db import transaction
from django.core.exceptions import ValidationError
from openoutreach.crm.models.lead import SuppressionRecord, Lead
from openoutreach.crm.models.deal import Deal, DealState

def suppress_lead_and_deals(linkedin_url, reason="Suppressed"):
    """
    Suppresses a lead by URL globally, rejects any active/pending deals for that lead
    to prevent draft generation, queue insertion, and sending, preserving all evidence.
    """
    with transaction.atomic():
        suppression_rec, created = SuppressionRecord.objects.get_or_create(
            linkedin_url=linkedin_url,
            defaults={"reason": reason}
        )
        if not created and reason:
            suppression_rec.reason = reason
            suppression_rec.save()

        # Update all active/pending deals for this lead URL
        deals = Deal.objects.filter(lead__linkedin_url=linkedin_url)
        for deal in deals:
            deal.manual_review_status = "rejected"
            deal.rejection_reason = f"Suppressed: {reason}"
            deal.sendable = False
            deal.external_action_authorized = False
            deal.save()
            
        return suppression_rec
