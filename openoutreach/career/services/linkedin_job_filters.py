"""LinkedIn Job Search Filter Mapper Service for HuntFlow."""

from openoutreach.career.models import JobSearchPreference


def build_linkedin_job_search_plan(preference: JobSearchPreference) -> dict:
    """Map JobSearchPreference into LinkedIn-supported filters and local HuntFlow filters."""
    keywords_str = " ".join(preference.keywords or []) if isinstance(preference.keywords, list) else str(preference.keywords or "")
    if not keywords_str and preference.job_titles:
        keywords_str = " ".join(preference.job_titles)

    locations_str = preference.locations[0] if (preference.locations and isinstance(preference.locations, list)) else ""

    linkedin_filters = {
        "keywords": keywords_str,
        "job_titles": preference.job_titles or [],
        "location": locations_str,
        "remote_options": preference.remote_options or [],
        "employment_types": preference.employment_types or [],
        "experience_levels": preference.experience_levels or [],
        "date_posted": preference.date_posted or "any_time",
        "easy_apply_only": bool(preference.easy_apply_only),
        "included_companies": preference.included_companies or [],
    }

    local_filters = {
        "excluded_companies": preference.excluded_companies or [],
        "included_industries": preference.included_industries or [],
        "excluded_industries": preference.excluded_industries or [],
        "minimum_salary": preference.minimum_salary,
        "currency": preference.currency or "USD",
        "work_authorization_required": bool(preference.work_authorization_required),
        "visa_sponsorship_required": bool(preference.visa_sponsorship_required),
        "match_threshold": preference.match_threshold,
        "execution_mode": preference.execution_mode,
    }

    unsupported = []
    if preference.minimum_salary:
        unsupported.append({"name": "minimum_salary", "reason": "Evaluated by HuntFlow after discovery"})
    if preference.visa_sponsorship_required:
        unsupported.append({"name": "visa_sponsorship_required", "reason": "Evaluated by HuntFlow after discovery"})
    if preference.work_authorization_required:
        unsupported.append({"name": "work_authorization_required", "reason": "Evaluated by HuntFlow after discovery"})
    if preference.excluded_companies:
        unsupported.append({"name": "excluded_companies", "reason": "Applied by HuntFlow after discovery"})
    if preference.excluded_industries:
        unsupported.append({"name": "excluded_industries", "reason": "Applied by HuntFlow after discovery"})

    return {
        "linkedin_filters": linkedin_filters,
        "local_filters": local_filters,
        "unsupported_filters": unsupported,
    }
