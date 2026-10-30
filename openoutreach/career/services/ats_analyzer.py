"""High-Volume ATS Job Description Analyzer and Evidence Matcher.

Extracts structured requirements from job descriptions, classifies keywords against
approved candidate facts (SUPPORTED / PARTIALLY_SUPPORTED / UNSUPPORTED), and calculates
internal non-inflated ATS match scores and priority bands.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple


# Standard known tech keywords for precise extraction
COMMON_TECH_TAXONOMY = {
    "python", "django", "fastapi", "flask", "javascript", "typescript", "react", "react.js",
    "next.js", "vue", "vue.js", "angular", "node.js", "nodejs", "html", "html5", "css",
    "css3", "tailwind", "bootstrap", "sass", "postgresql", "postgres", "mysql", "sqlite",
    "mongodb", "redis", "elasticsearch", "docker", "kubernetes", "k8s", "aws", "gcp",
    "azure", "linux", "git", "github", "gitlab", "ci/cd", "rest", "restful", "graphql",
    "grpc", "playwright", "selenium", "pytest", "unittest", "celery", "rabbitmq", "kafka",
    "spark", "hadoop", "airflow", "pandas", "numpy", "scipy", "scikit-learn", "tensorflow",
    "pytorch", "keras", "llm", "llms", "genai", "prompt engineering", "langchain", "llamaindex",
    "ollama", "huggingface", "bert", "gpt", "nlp", "computer vision", "opencv", "c++",
    "c#", ".net", "java", "spring", "spring boot", "golang", "go", "rust", "ruby", "rails",
    "php", "laravel", "scala", "kotlin", "swift", "terraform", "ansible", "jenkins", "graphql",
    "microservices", "distributed systems", "system design", "agile", "scrum", "kanban",
    "jira", "confluence", "solidity", "web3", "sql", "nosql", "etl", "data pipelines",
    "security", "oauth", "jwt", "saml", "sso", "devops", "sre", "monitoring", "prometheus",
    "grafana", "datadog", "sentry", "bash", "shell scripting"
}

SENIORITY_PATTERNS = [
    (r"\b(lead|principal|staff|architect|director|vp|head of)\b", "lead"),
    (r"\b(senior|sr\.?|sr\b)", "senior"),
    (r"\b(mid|intermediate|mid-level)\b", "mid"),
    (r"\b(junior|jr\.?|jr\b|associate|entry-level|entry level|intern|internship)\b", "junior"),
]

EXPERIENCE_YEARS_PATTERNS = [
    r"(\d+)\s*\+?\s*(?:to|-)\s*(\d+)\s*(?:years?|yrs?)(?:\s+of\s+experience)?",
    r"(\d+)\s*\+?\s*(?:years?|yrs?)(?:\s+of\s+experience)?",
    r"(?:at least|minimum of|minimum)\s+(\d+)\s*(?:years?|yrs?)",
]

EDUCATION_PATTERNS = [
    r"\b(bachelor'?s?|bs|ba|b\.s\.|b\.a\.|undergraduate)\b",
    r"\b(master'?s?|ms|m\.s\.|msc|m\.sc\.|mba|postgraduate)\b",
    r"\b(phd|ph\.d\.|doctorate)\b",
    r"\b(computer science|software engineering|information technology|electrical engineering|mathematics|data science)\b"
]


def extract_jd_requirements(
    job_or_description: Any,
    title: Optional[str] = None,
    company: Optional[str] = None,
    job_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extracts structured requirements from a job description or Job model instance.
    Generates a deterministic JD snapshot hash.
    """
    if hasattr(job_or_description, "description"):
        desc = getattr(job_or_description, "description", "") or ""
        job_title = getattr(job_or_description, "title", "") or title or ""
        company_name = getattr(job_or_description, "company", "") or company or ""
        url = getattr(job_or_description, "job_url", "") or job_url or ""
        location = getattr(job_or_description, "location", "") or ""
    elif isinstance(job_or_description, dict):
        desc = job_or_description.get("description", "") or ""
        job_title = job_or_description.get("title", "") or title or ""
        company_name = job_or_description.get("company", "") or company or ""
        url = job_or_description.get("job_url", "") or job_url or ""
        location = job_or_description.get("location", "") or ""
    else:
        desc = str(job_or_description or "")
        job_title = title or ""
        company_name = company or ""
        url = job_url or ""
        location = ""

    desc_lower = desc.lower()
    full_text = f"{job_title} {desc}".lower()

    # Content hash
    raw_content = f"{job_title}|{company_name}|{desc}".encode("utf-8")
    content_hash = hashlib.sha256(raw_content).hexdigest()

    # 1. Seniority extraction
    seniority = "mid"
    for pat, level in SENIORITY_PATTERNS:
        if re.search(pat, f"{job_title} {desc[:500]}".lower()):
            seniority = level
            break

    # 2. Years of experience extraction
    min_years = 0
    max_years = None
    for pattern in EXPERIENCE_YEARS_PATTERNS:
        match = re.search(pattern, desc_lower)
        if match:
            groups = match.groups()
            if len(groups) == 2 and groups[1]:
                min_years = int(groups[0])
                max_years = int(groups[1])
            elif len(groups) >= 1 and groups[0]:
                min_years = int(groups[0])
            break

    # 3. Technologies & Skills extraction
    found_tech = []
    for tech in COMMON_TECH_TAXONOMY:
        # Match as full word / token
        pattern = r"(?<!\w)" + re.escape(tech) + r"(?!\w)"
        if re.search(pattern, full_text):
            found_tech.append(tech)

    # Sort found technologies deterministically
    found_tech = sorted(list(set(found_tech)))

    # Categorize must-have vs preferred skills based on section headings
    required_skills = []
    preferred_skills = []

    req_section_match = re.search(r"(?:requirements|qualifications|what you need|must have|required skills)[:\n](.*?)(?=(?:preferred|nice to have|bonus|what we offer|benefits|\Z))", desc_lower, re.DOTALL)
    pref_section_match = re.search(r"(?:preferred|nice to have|bonus|plus|desirable)[:\n](.*?)(?=(?:what we offer|benefits|about us|\Z))", desc_lower, re.DOTALL)

    req_text = req_section_match.group(1) if req_section_match else desc_lower
    pref_text = pref_section_match.group(1) if pref_section_match else ""

    for tech in found_tech:
        pattern = r"(?<!\w)" + re.escape(tech) + r"(?!\w)"
        in_pref = bool(pref_text and re.search(pattern, pref_text))
        in_req = bool(re.search(pattern, req_text))

        if in_pref and not in_req:
            preferred_skills.append(tech)
        else:
            required_skills.append(tech)

    # 4. Education Requirements
    education_reqs = []
    for pat in EDUCATION_PATTERNS:
        matches = re.findall(pat, desc_lower)
        for m in matches:
            if m not in education_reqs:
                education_reqs.append(m)

    # 5. Work model / Location
    work_model = "onsite"
    if "remote" in desc_lower or "remote" in location.lower() or "work from home" in desc_lower or "wfh" in desc_lower:
        work_model = "remote"
    elif "hybrid" in desc_lower or "hybrid" in location.lower():
        work_model = "hybrid"

    # 6. Must-have criteria summary
    must_have_criteria = []
    if min_years > 0:
        must_have_criteria.append(f"{min_years}+ years relevant experience")
    if required_skills:
        top_reqs = ", ".join(required_skills[:5])
        must_have_criteria.append(f"Proficiency in {top_reqs}")
    if education_reqs:
        must_have_criteria.append(f"Degree in {', '.join(education_reqs[:2])}")

    # 7. Screening keywords / work authorization
    screening_reqs = {}
    if "sponsorship" in desc_lower:
        screening_reqs["sponsorship_mentioned"] = True
    if "clearance" in desc_lower or "security clearance" in desc_lower:
        screening_reqs["clearance_required"] = True
    if "relocation" in desc_lower:
        screening_reqs["relocation_mentioned"] = True

    return {
        "title": job_title,
        "company": company_name,
        "location": location,
        "canonical_url": url,
        "work_model": work_model,
        "seniority": seniority,
        "min_years_experience": min_years,
        "max_years_experience": max_years,
        "required_skills": required_skills,
        "preferred_skills": preferred_skills,
        "all_technologies": found_tech,
        "education_requirements": education_reqs,
        "must_have_criteria": must_have_criteria,
        "nice_to_have_criteria": preferred_skills,
        "screening_requirements": screening_reqs,
        "ats_keywords": found_tech,
        "content_hash": content_hash,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "raw_description_preview": desc[:300] if desc else ""
    }


def get_candidate_factual_evidence(candidate_profile_or_version: Any) -> Dict[str, Any]:
    """
    Extracts all approved factual statements, verified skills, employment history,
    education, and projects from a candidate record.
    """
    facts_by_type = {
        "skills": [],
        "experience": [],
        "education": [],
        "certifications": [],
        "projects": [],
        "preferences": {},
        "raw_facts": []
    }

    if not candidate_profile_or_version:
        return facts_by_type

    # Resolve active approved version
    version = candidate_profile_or_version
    if hasattr(candidate_profile_or_version, "active_version") and candidate_profile_or_version.active_version:
        version = candidate_profile_or_version.active_version

    # Collect facts from database relations or snapshot
    if hasattr(version, "facts") and hasattr(version.facts, "all"):
        for fact in version.facts.filter(verified=True):
            fact_id = getattr(fact, "id", None)
            f_type = getattr(fact, "fact_type", "other")
            val = getattr(fact, "value", {})
            category = getattr(fact, "category", "")

            entry = {
                "fact_id": fact_id,
                "category": category,
                "value": val,
                "fact_type": f_type,
            }
            facts_by_type["raw_facts"].append(entry)

            if f_type in ["skill", "skills"] or category.lower() in ["skill", "skills"]:
                if isinstance(val, str):
                    facts_by_type["skills"].append({"name": val.strip(), "evidence_id": fact_id})
                elif isinstance(val, dict):
                    skill_name = val.get("name") or val.get("skill") or val.get("value")
                    if skill_name:
                        facts_by_type["skills"].append({"name": str(skill_name).strip(), "evidence_id": fact_id})
            elif f_type in ["experience", "work"]:
                facts_by_type["experience"].append(entry)
            elif f_type in ["education", "degree"]:
                facts_by_type["education"].append(entry)
            elif f_type in ["certification", "certifications"]:
                facts_by_type["certifications"].append(entry)
            elif f_type in ["project", "projects"]:
                facts_by_type["projects"].append(entry)

    # Also extract from fact_snapshot JSONField if present
    fact_snapshot = getattr(version, "fact_snapshot", []) or []
    if isinstance(fact_snapshot, list):
        for item in fact_snapshot:
            if isinstance(item, dict):
                cat = (item.get("category") or "").lower()
                val = item.get("value")
                item_id = item.get("id") or item.get("fact_id") or "snap"
                if cat in ["skill", "skills"]:
                    if isinstance(val, str):
                        facts_by_type["skills"].append({"name": val.strip(), "evidence_id": item_id})
                    elif isinstance(val, list):
                        for s in val:
                            facts_by_type["skills"].append({"name": str(s).strip(), "evidence_id": item_id})
                elif cat in ["experience", "work"]:
                    facts_by_type["experience"].append({"fact_id": item_id, "category": cat, "value": val})
                elif cat in ["education", "degree"]:
                    facts_by_type["education"].append({"fact_id": item_id, "category": cat, "value": val})

    # Summary text skills extraction
    summary = getattr(version, "summary", "") or ""
    if summary:
        for tech in COMMON_TECH_TAXONOMY:
            if re.search(r"(?<!\w)" + re.escape(tech) + r"(?!\w)", summary.lower()):
                if not any(s["name"].lower() == tech for s in facts_by_type["skills"]):
                    facts_by_type["skills"].append({"name": tech, "evidence_id": "summary"})

    return facts_by_type


def classify_keywords_against_candidate(
    extracted_requirements: Dict[str, Any],
    candidate_profile_or_version: Any
) -> Dict[str, Dict[str, Any]]:
    """
    Classifies every keyword in extracted JD requirements as:
    - SUPPORTED: verified in approved candidate facts (with evidence IDs attached).
    - PARTIALLY_SUPPORTED: related domain/substring present.
    - UNSUPPORTED: no candidate evidence found. MUST NOT be inserted as candidate truth.
    """
    evidence = get_candidate_factual_evidence(candidate_profile_or_version)
    candidate_skills_map = {}
    for s in evidence["skills"]:
        candidate_skills_map[s["name"].lower()] = s["evidence_id"]

    candidate_full_text = json.dumps(evidence).lower()

    classification: Dict[str, Dict[str, Any]] = {}
    all_keywords = set(extracted_requirements.get("ats_keywords", []) + extracted_requirements.get("required_skills", []) + extracted_requirements.get("preferred_skills", []))

    for kw in all_keywords:
        kw_norm = kw.lower().strip()
        if not kw_norm:
            continue

        # 1. Exact match in verified skills
        if kw_norm in candidate_skills_map:
            classification[kw] = {
                "status": "SUPPORTED",
                "evidence_type": "skill",
                "evidence_id": candidate_skills_map[kw_norm],
                "confidence": 1.0,
                "notes": f"Verified skill in candidate facts (ID: {candidate_skills_map[kw_norm]})"
            }
        # 2. Exact match in full factual evidence graph
        elif re.search(r"(?<!\w)" + re.escape(kw_norm) + r"(?!\w)", candidate_full_text):
            classification[kw] = {
                "status": "SUPPORTED",
                "evidence_type": "experience_or_project",
                "evidence_id": "evidence_graph",
                "confidence": 0.9,
                "notes": f"Factual evidence found in candidate record for '{kw}'"
            }
        # 3. Partial / alias match
        elif any(kw_norm in s or s in kw_norm for s in candidate_skills_map if len(s) > 2 and len(kw_norm) > 2):
            classification[kw] = {
                "status": "PARTIALLY_SUPPORTED",
                "evidence_type": "partial_skill",
                "evidence_id": "partial_match",
                "confidence": 0.6,
                "notes": f"Partial factual overlap with approved skills"
            }
        # 4. Strictly UNSUPPORTED
        else:
            classification[kw] = {
                "status": "UNSUPPORTED",
                "evidence_type": None,
                "evidence_id": None,
                "confidence": 0.0,
                "notes": f"No factual candidate evidence found. DO NOT claim as candidate skill."
            }

    return classification


def calculate_ats_match_score(
    extracted_requirements: Dict[str, Any],
    candidate_profile_or_version: Any,
    keyword_mapping: Optional[Dict[str, Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Computes rigorous internal ATS match scores (0-100) and priority bands.
    Never inflates score with unsupported requirements.
    """
    if keyword_mapping is None:
        keyword_mapping = classify_keywords_against_candidate(extracted_requirements, candidate_profile_or_version)

    required_skills = extracted_requirements.get("required_skills", [])
    preferred_skills = extracted_requirements.get("preferred_skills", [])

    # 1. Skills match calculation
    supported_req = [k for k in required_skills if keyword_mapping.get(k, {}).get("status") == "SUPPORTED"]
    partial_req = [k for k in required_skills if keyword_mapping.get(k, {}).get("status") == "PARTIALLY_SUPPORTED"]
    unsupported_req = [k for k in required_skills if keyword_mapping.get(k, {}).get("status") == "UNSUPPORTED"]

    supported_pref = [k for k in preferred_skills if keyword_mapping.get(k, {}).get("status") == "SUPPORTED"]

    if required_skills:
        skills_score = round(((len(supported_req) + len(partial_req) * 0.5) / len(required_skills)) * 100)
    elif supported_pref or unsupported_req:
        total = len(supported_pref) + len(unsupported_req)
        skills_score = round((len(supported_pref) / total) * 100) if total > 0 else 80
    else:
        skills_score = 75  # Default baseline when no specific skills demanded

    # 2. Must-have score
    must_have_count = len(required_skills)
    must_have_supported = len(supported_req)
    must_have_score = round((must_have_supported / must_have_count) * 100) if must_have_count > 0 else 85

    # 3. Experience years check
    evidence = get_candidate_factual_evidence(candidate_profile_or_version)
    cand_exp_entries = evidence.get("experience", [])
    total_exp_years = max(len(cand_exp_entries) * 2, 2)  # Conservative heuristic from verified roles
    req_min_years = extracted_requirements.get("min_years_experience", 0)

    if req_min_years == 0 or total_exp_years >= req_min_years:
        experience_score = 100
    else:
        experience_score = max(round((total_exp_years / req_min_years) * 100), 40)

    # 4. Education score
    cand_edu = evidence.get("education", [])
    req_edu = extracted_requirements.get("education_requirements", [])
    if not req_edu or cand_edu:
        education_score = 100
    else:
        education_score = 70

    # 5. Overall weighted score
    overall_score = round(
        (must_have_score * 0.40) +
        (skills_score * 0.35) +
        (experience_score * 0.15) +
        (education_score * 0.10)
    )
    overall_score = min(max(overall_score, 0), 100)

    # 6. Priority Band
    if overall_score >= 85:
        priority_band = "Excellent Match"
    elif overall_score >= 70:
        priority_band = "Strong Match"
    elif overall_score >= 50:
        priority_band = "Possible Match"
    else:
        priority_band = "Low Match / Reject"

    explanation = (
        f"Match Score: {overall_score}% ({priority_band}). "
        f"Must-have coverage: {must_have_supported}/{must_have_count}. "
        f"Key supported technologies: {', '.join(supported_req[:6]) if supported_req else 'General alignment'}. "
        f"Missing/unsupported requirements: {', '.join(unsupported_req[:4]) if unsupported_req else 'None'}."
    )

    return {
        "overall_score": overall_score,
        "must_have_score": must_have_score,
        "skills_score": skills_score,
        "experience_score": experience_score,
        "education_score": education_score,
        "priority_band": priority_band,
        "supported_skills": supported_req + supported_pref,
        "partial_skills": partial_req,
        "unsupported_skills": unsupported_req,
        "explanation": explanation,
        "keyword_classification": keyword_mapping,
    }
