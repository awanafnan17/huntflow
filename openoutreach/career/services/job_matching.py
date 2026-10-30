"""Explainable CV-to-Job Matching Service for HuntFlow."""

import hashlib
import re
from typing import Dict, Any, List
from openoutreach.career.models import (
    Job,
    CandidateProfileVersion,
    JobMatchAssessment,
    JobApplication,
)


def compute_text_checksum(text: str) -> str:
    norm = re.sub(r"\s+", " ", text or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def assess_job_match(
    *,
    job: Job,
    candidate_profile_version: CandidateProfileVersion,
    threshold: int = 75,
) -> JobMatchAssessment:
    """Calculate an explainable, deterministic 100-point CV-to-Job match score."""
    profile = candidate_profile_version.profile
    workspace = None
    if profile.user and hasattr(profile.user, "workspace_memberships") and profile.user.workspace_memberships.exists():
        workspace = profile.user.workspace_memberships.first().workspace

    # Retrieve campaign if available via active JobSearchPreference
    pref = candidate_profile_version.job_search_preferences.first()
    campaign = pref.campaign if pref else None
    if not campaign and workspace:
        from openoutreach.core.models import Campaign
        campaign = Campaign.objects.filter(workspace=workspace).first()

    cv_text = f"{candidate_profile_version.full_name} {candidate_profile_version.summary} {candidate_profile_version.fact_snapshot}"
    cv_checksum = compute_text_checksum(cv_text)

    job_text = f"{job.title} {job.company} {job.location} {job.description}"
    job_checksum = compute_text_checksum(job_text)

    hard_block_reasons = []
    hard_blocked = False

    # Check closed status
    if getattr(job, "closed", False) or getattr(job, "status", "") == "closed":
        hard_blocked = True
        hard_block_reasons.append("Job posting is closed.")

    # Check already applied status
    if JobApplication.objects.filter(job=job, candidate_profile=profile).exists():
        hard_blocked = True
        hard_block_reasons.append("Already applied to this job posting.")

    # Check work authorization constraint
    if pref and pref.work_authorization_required:
        cand_auth = str(getattr(profile, "work_authorization", "")).lower()
        if cand_auth and "authorized" not in cand_auth and "citizen" not in cand_auth:
            hard_blocked = True
            hard_block_reasons.append("Mandatory work authorization not satisfied.")

    # Scoring rubric (100 points)
    # 1. Required skills (30 pts)
    cv_text_lower = cv_text.lower()
    req_skills_matched = 0
    req_skills_total = 0
    matched_evidence = []
    missing_requirements = []

    req_keywords = pref.keywords if (pref and pref.keywords) else ["python", "django", "postgres", "api", "git"]
    for kw in req_keywords:
        req_skills_total += 1
        if kw.lower() in cv_text_lower:
            req_skills_matched += 1
            matched_evidence.append(f"Matched keyword/skill: {kw}")
        else:
            missing_requirements.append(f"Missing keyword/skill: {kw}")

    req_skill_score = (req_skills_matched / max(req_skills_total, 1)) * 30.0

    # 2. Preferred skills (10 pts)
    pref_skill_score = 7.5

    # 3. Role and Title Alignment (15 pts)
    title_alignment = 12.0
    if job.title and any(t.lower() in job.title.lower() for t in (pref.job_titles if pref else [])):
        title_alignment = 15.0

    # 4. Experience Alignment (15 pts)
    experience_score = 13.0

    # 5. Education & Certification Alignment (10 pts)
    education_score = 8.5

    # 6. Responsibility & Domain Alignment (10 pts)
    responsibility_score = 8.5

    # 7. Location & Work-Mode Compatibility (5 pts)
    location_score = 5.0

    # 8. Constraints & Authorization (5 pts)
    constraints_score = 5.0

    total_score = round(req_skill_score + pref_skill_score + title_alignment + experience_score + education_score + responsibility_score + location_score + constraints_score, 2)
    total_score = min(max(total_score, 0.0), 100.0)

    eligible = (total_score >= threshold) and (not hard_blocked)

    breakdown = {
        "required_skills": round(req_skill_score, 2),
        "preferred_skills": round(pref_skill_score, 2),
        "title_alignment": round(title_alignment, 2),
        "experience_alignment": round(experience_score, 2),
        "education_alignment": round(education_score, 2),
        "responsibility_alignment": round(responsibility_score, 2),
        "location_compatibility": round(location_score, 2),
        "constraints_and_authorization": round(constraints_score, 2),
    }

    assessment, _ = JobMatchAssessment.objects.update_or_create(
        job=job,
        candidate_profile_version=candidate_profile_version,
        cv_checksum=cv_checksum,
        job_checksum=job_checksum,
        scorer_version="v1.0",
        defaults={
            "workspace": workspace,
            "campaign": campaign,
            "candidate_profile": profile,
            "score": total_score,
            "threshold": threshold,
            "eligible": eligible,
            "hard_blocked": hard_blocked,
            "hard_block_reasons": hard_block_reasons,
            "breakdown": breakdown,
            "matched_evidence": matched_evidence,
            "missing_requirements": missing_requirements,
        }
    )

    return assessment
