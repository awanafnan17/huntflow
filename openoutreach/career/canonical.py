import json
import hashlib

def canonicalize_fact_data(fact_list):
    """
    Given a list of facts (which can be drafts, CandidateFact objects, or JSON dictionaries),
    returns a deterministic UTF-8 bytes payload for checksumming.
    """
    normalized_items = []
    for item in fact_list:
        if hasattr(item, "fact_type"):  # Django model (CandidateFactDraft or CandidateFact)
            fact_type = item.fact_type
            category = item.category or ""
            value = item.value or {}
        else:  # dict from JSON snapshot
            fact_type = item.get("fact_type", "")
            category = item.get("category", "") or ""
            value = item.get("value") or {}

        # Normalize value fields (dictionary key sorting, normalize nulls and strip strings)
        normalized_value = {}
        if isinstance(value, dict):
            for k, v in value.items():
                if v is None:
                    continue
                if isinstance(v, list):
                    normalized_value[k] = [str(x).strip() for x in v if x is not None]
                else:
                    normalized_value[k] = str(v).strip()
        elif isinstance(value, list):
            # Fallback if value is a list
            normalized_value = [str(x).strip() for x in value if x is not None]
        else:
            normalized_value = str(value).strip()

        normalized_items.append({
            "fact_type": str(fact_type).strip(),
            "category": str(category).strip(),
            "value": normalized_value
        })

    # Stable fact ordering (by fact_type first, then category, then sorted value string)
    def sort_key(x):
        return (
            x["fact_type"],
            x["category"],
            json.dumps(x["value"], sort_keys=True, ensure_ascii=False)
        )
    normalized_items.sort(key=sort_key)

    # Deterministic UTF-8 JSON serialization
    serialized = json.dumps(normalized_items, sort_keys=True, ensure_ascii=False)
    return serialized.encode("utf-8")

def compute_canonical_checksum(fact_list):
    data = canonicalize_fact_data(fact_list)
    return hashlib.sha256(data).hexdigest()
