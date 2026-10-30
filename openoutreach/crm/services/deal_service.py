# openoutreach/crm/services/deal_service.py
from django.core.exceptions import ValidationError
from openoutreach.crm.models.deal import Deal, DealState

def create_deal(campaign, lead, state=DealState.QUALIFIED, **kwargs):
    """
    Service helper for creating a Deal with strict workspace consistency checks.
    """
    if campaign.workspace != lead.workspace:
        raise ValidationError("Lead and Campaign must belong to the same Workspace.")
    
    deal = Deal(campaign=campaign, lead=lead, state=state, **kwargs)
    deal.full_clean()
    deal.save()
    return deal

def update_deal(deal: Deal, campaign=None, lead=None, **kwargs):
    """
    Service helper for updating a Deal with strict workspace consistency checks.
    """
    new_campaign = campaign if campaign is not None else deal.campaign
    new_lead = lead if lead is not None else deal.lead

    if new_campaign.workspace != new_lead.workspace:
        raise ValidationError("Lead and Campaign must belong to the same Workspace.")

    deal.campaign = new_campaign
    deal.lead = new_lead
    for attr, val in kwargs.items():
        setattr(deal, attr, val)

    deal.full_clean()
    deal.save()
    return deal
