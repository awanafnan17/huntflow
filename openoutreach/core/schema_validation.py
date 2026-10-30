# openoutreach/core/schema_validation.py
import re
from django.core.exceptions import ValidationError

DANGEROUS_PATTERNS = [
    r"__import__",
    r"__subclasses__",
    r"__class__",
    r"__globals__",
    r"eval\(",
    r"exec\(",
    r"os\.system",
    r"subprocess",
    r"<script>",
    r"javascript:",
]

def validate_campaign_context_schema(context_data: dict, schema: dict) -> tuple[bool, list[str]]:
    """
    Pure Python JSON structure validator.
    Validates required fields, types, length bounds, and blocks dangerous injection constructs.
    """
    errors = []
    if not isinstance(context_data, dict):
        return False, ["Context data must be a dictionary."]

    # Check for dangerous injection constructs in string values
    for k, v in context_data.items():
        if isinstance(v, str):
            for pattern in DANGEROUS_PATTERNS:
                if re.search(pattern, v, re.IGNORECASE):
                    errors.append(f"Field '{k}' contains prohibited dangerous construct matching pattern '{pattern}'.")

    if not schema:
        return len(errors) == 0, errors

    required = schema.get("required_fields", [])
    for req in required:
        if req not in context_data or context_data[req] is None or context_data[req] == "":
            errors.append(f"Missing required field: {req}")

    field_types = schema.get("field_types", {})
    type_map = {"str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict}
    for field, type_name in field_types.items():
        if field in context_data and context_data[field] is not None:
            expected_cls = type_map.get(type_name)
            if expected_cls and not isinstance(context_data[field], expected_cls):
                errors.append(f"Field '{field}' must be of type {type_name}.")

    max_lengths = schema.get("max_lengths", {})
    for field, max_len in max_lengths.items():
        val = context_data.get(field)
        if isinstance(val, str) and len(val) > max_len:
            errors.append(f"Field '{field}' exceeds maximum length of {max_len} characters.")

    return len(errors) == 0, errors
