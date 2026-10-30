"""Automated Document Quality Assurance and Hallucination Gate.

Validates candidate identity, company/job specificity, absence of template leaks,
truthfulness of skills, and programmatic text extractability from generated DOCX/PDF artifacts.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

import docx
import pypdf


TEMPLATE_LEAK_PATTERNS = [
    r"\{\{.*?\}\}",
    r"\[insert\b.*?\]",
    r"\[company\b.*?\]",
    r"\[title\b.*?\]",
    r"\[candidate\b.*?\]",
    r"<insert\b.*?>",
    r"\bTODO\b",
    r"\bFIXME\b",
    r"\bUNDEFINED\b",
    r"\bNULL\b",
]

REQUIRED_ATS_SECTIONS = [
    "PROFESSIONAL SUMMARY",
    "TECHNICAL SKILLS",
    "PROFESSIONAL EXPERIENCE",
    "EDUCATION"
]


def extract_text_from_docx(file_path: str) -> str:
    """Extracts plain text from a DOCX file."""
    if not os.path.exists(file_path):
        return ""
    doc = docx.Document(file_path)
    full_text = []
    for p in doc.paragraphs:
        if p.text:
            full_text.append(p.text)
    return "\n".join(full_text)


def extract_text_from_pdf(file_path: str) -> str:
    """Extracts plain text from a text-selectable PDF file."""
    if not os.path.exists(file_path):
        return ""
    text_content = []
    try:
        reader = pypdf.PdfReader(file_path)
        for page in reader.pages:
            text = page.extract_text()
            if text:
                text_content.append(text)
    except Exception:
        return ""
    return "\n".join(text_content)


def qa_application_package(
    candidate: Any,
    job_requirements: Dict[str, Any],
    resume_text: str,
    cover_letter_text: Optional[str] = None,
    keyword_mapping: Optional[Dict[str, Dict[str, Any]]] = None,
    resume_docx_path: Optional[str] = None,
    resume_pdf_path: Optional[str] = None,
    cover_letter_docx_path: Optional[str] = None,
    cover_letter_pdf_path: Optional[str] = None,
    other_jobs_context: Optional[List[Dict[str, Any]]] = None
) -> Tuple[bool, List[str], Dict[str, Any]]:
    """
    Executes a comprehensive, multi-point document QA check on an application package.
    Returns:
    - passed (bool): True if all checks pass.
    - errors (list[str]): List of critical errors if failed.
    - metrics (dict): QA metrics and scores.
    """
    errors: List[str] = []
    metrics: Dict[str, Any] = {
        "candidate_identity_valid": True,
        "template_leak_free": True,
        "unsupported_claims_count": 0,
        "cross_job_contamination_free": True,
        "ats_structure_valid": True,
        "docx_readable": True,
        "pdf_readable": True,
        "docx_extracted_char_count": 0,
        "pdf_extracted_char_count": 0,
    }

    cand_name = getattr(candidate, "full_name", "") or ""
    cand_email = getattr(candidate, "email", "") or ""
    target_company = job_requirements.get("company", "") or ""
    target_title = job_requirements.get("title", "") or ""

    # 1. Candidate Identity QA
    if cand_name and cand_name.lower() not in resume_text.lower():
        errors.append(f"Candidate name '{cand_name}' missing from resume header.")
        metrics["candidate_identity_valid"] = False

    if cover_letter_text and cand_name and cand_name.lower() not in cover_letter_text.lower():
        errors.append(f"Candidate name '{cand_name}' missing from cover letter.")
        metrics["candidate_identity_valid"] = False

    # 2. Template Variable Leak Detection
    combined_docs = f"{resume_text}\n{cover_letter_text or ''}"
    for pat in TEMPLATE_LEAK_PATTERNS:
        match = re.search(pat, combined_docs, re.IGNORECASE)
        if match:
            errors.append(f"Template variable or placeholder leak detected: '{match.group(0)}'")
            metrics["template_leak_free"] = False

    # 3. Cross-Job Contamination Detection
    if other_jobs_context:
        for other in other_jobs_context:
            other_co = other.get("company", "").strip()
            other_title = other.get("title", "").strip()
            if other_co and other_co.lower() != target_company.lower() and len(other_co) > 3:
                if other_co.lower() in combined_docs.lower():
                    errors.append(f"Cross-job contamination: Found other company name '{other_co}' in package for '{target_company}'")
                    metrics["cross_job_contamination_free"] = False

    # Target company should be in cover letter if cover letter provided
    if cover_letter_text and target_company and len(target_company) > 2:
        if target_company.lower() not in cover_letter_text.lower():
            errors.append(f"Target company '{target_company}' missing from tailored cover letter.")

    # 4. Strict Hallucination & Unsupported Skills Check
    if keyword_mapping:
        unsupported_skills = [
            kw for kw, info in keyword_mapping.items()
            if info.get("status") == "UNSUPPORTED"
        ]
        # Check if any unsupported skill is claimed in technical skills section
        tech_skills_section_match = re.search(r"TECHNICAL SKILLS(.*?)(?=PROFESSIONAL EXPERIENCE|\Z)", resume_text, re.DOTALL)
        tech_skills_text = tech_skills_section_match.group(1).lower() if tech_skills_section_match else ""

        for un_kw in unsupported_skills:
            if len(un_kw) > 2:
                # Word boundary check
                pattern = r"(?<!\w)" + re.escape(un_kw.lower()) + r"(?!\w)"
                if re.search(pattern, tech_skills_text):
                    errors.append(f"Fabrication detected: Unsupported skill '{un_kw}' was inserted into technical skills.")
                    metrics["unsupported_claims_count"] += 1

    # 5. ATS Structural QA
    for sec in REQUIRED_ATS_SECTIONS:
        if sec not in resume_text:
            errors.append(f"Standard ATS section header '{sec}' is missing from resume.")
            metrics["ats_structure_valid"] = False

    # 6. Physical DOCX Artifact QA
    if resume_docx_path:
        if not os.path.exists(resume_docx_path) or os.path.getsize(resume_docx_path) == 0:
            errors.append(f"Generated DOCX file is missing or 0 bytes: {resume_docx_path}")
            metrics["docx_readable"] = False
        else:
            docx_text = extract_text_from_docx(resume_docx_path)
            metrics["docx_extracted_char_count"] = len(docx_text)
            if len(docx_text) < 100:
                errors.append(f"DOCX text extraction failed or insufficient text ({len(docx_text)} chars).")
                metrics["docx_readable"] = False

    # 7. Physical PDF Artifact QA
    if resume_pdf_path:
        if not os.path.exists(resume_pdf_path) or os.path.getsize(resume_pdf_path) == 0:
            errors.append(f"Generated PDF file is missing or 0 bytes: {resume_pdf_path}")
            metrics["pdf_readable"] = False
        else:
            pdf_text = extract_text_from_pdf(resume_pdf_path)
            metrics["pdf_extracted_char_count"] = len(pdf_text)
            if len(pdf_text) < 100:
                errors.append(f"PDF text extraction failed or text is not selectable ({len(pdf_text)} chars).")
                metrics["pdf_readable"] = False

    passed = len(errors) == 0
    return passed, errors, metrics
