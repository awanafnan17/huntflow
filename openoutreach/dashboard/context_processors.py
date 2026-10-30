# openoutreach/dashboard/context_processors.py
from __future__ import annotations

import logging
from openoutreach.core.models import OperatorAlert

logger = logging.getLogger(__name__)


def inbox_badge_processor(request):
    """Safe context processor to return count of unacknowledged active alerts.
    
    Performs only a cheap count read query.
    Fails closed with count 0 in case of any exceptions.
    Handles anonymous visitors.
    """
    if not hasattr(request, "user") or not request.user or not request.user.is_authenticated:
        return {"inbox_unread_count": 0}
        
    try:
        # Fetch simple unacknowledged active count query
        count = OperatorAlert.objects.filter(
            status=OperatorAlert.AlertStatus.ACTIVE
        ).count()
        return {"inbox_unread_count": count}
    except Exception as e:
        logger.warning("Error computing inbox badge count context: %s", e)
        return {"inbox_unread_count": 0}
