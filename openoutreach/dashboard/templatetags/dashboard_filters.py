# openoutreach/dashboard/templatetags/dashboard_filters.py
from __future__ import annotations

import json
from django import template

register = template.Library()


@register.filter(name="json_dumps_pretty")
def json_dumps_pretty(value):
    """Safe formatting of payloads and metadata in views."""
    try:
        if isinstance(value, str):
            value = json.loads(value)
        return json.dumps(value, indent=2, default=str)
    except Exception:
        return str(value)
