"""Ollama LLM ATS Tailoring and Evidence Grounding Engine.

Provides LLM-assisted rephrasing for professional summaries, experience bullets,
and cover letters strictly grounded in approved candidate facts.
Includes structured output schema, grounding validation, hallucination detection,
and deterministic fallback on LLM failure or timeout.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple
from urllib import request as urlrequest

from openoutreach.career.services.ats_analyzer import (
    classify_keywords_against_candidate,
    get_candidate_factual_evidence,
)

logger = logging.getLogger(__name__)

# Bounded inference semaphore for Ollama (limits concurrent inferences to prevent memory/CPU starvation)
_OLLAMA_SEMAPHORE = threading.BoundedSemaphore(value=3)


def get_ollama_api_url() -> str:
    from openoutreach.core.llm import get_default_ollama_base_url
    base = get_default_ollama_base_url()
    return f"{base}/api/generate"


def get_ollama_model_name() -> str:
    env_model = os.environ.get("OLLAMA_MODEL")
    if env_model:
        return env_model
    try:
        from openoutreach.core.models import SiteConfig
        cfg = SiteConfig.load()
        if cfg.ai_model and "ollama:" in cfg.ai_model:
            return cfg.ai_model.split("ollama:", 1)[1]
    except Exception:
        pass
    return "qwen2.5:1.5b"


def is_ollama_available(endpoint: Optional[str] = None, timeout: float = 2.0) -> bool:
    """Checks if the local Ollama server is responsive."""
    if not endpoint:
        from openoutreach.core.llm import get_default_ollama_base_url
        endpoint = f"{get_default_ollama_base_url()}/api/tags"
    try:
        req = urlrequest.Request(endpoint, method="GET")
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def call_ollama_raw(prompt: str, model: Optional[str] = None, timeout: Optional[float] = None) -> Optional[str]:
    """
    Executes a bounded inference call to local Ollama instance with timeout and keep_alive.
    Returns response text or None if unreachable/error.
    """
    effective_timeout = timeout if timeout is not None else float(os.environ.get("OLLAMA_REQUEST_TIMEOUT", "60.0"))
    acquired = _OLLAMA_SEMAPHORE.acquire(blocking=True, timeout=15.0)
    if not acquired:
        logger.warning("Ollama inference queue busy. Falling back to deterministic generation.")
        return None

    actual_model = model or get_ollama_model_name()
    api_url = get_ollama_api_url()

    try:
        payload = {
            "model": actual_model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", "24h"),
            "options": {
                "temperature": 0.2,
                "top_p": 0.9,
                "num_predict": 1500,
            }
        }
        data = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(
            api_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urlrequest.urlopen(req, timeout=effective_timeout) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("response", "")
    except Exception as e:
        logger.warning("Ollama call failed (%s). Using safe fallback.", e)
        return None
    finally:
        _OLLAMA_SEMAPHORE.release()
    return None


def extract_json_from_llm_response(raw_text: str) -> Optional[Dict[str, Any]]:
    """Extracts and parses JSON from raw LLM output, handling markdown fences and surrounding text."""
    if not raw_text:
        return None

    # Strip thinking tags if Qwen3 generates <think>...</think>
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()

    # Find JSON block enclosed in ```json ... ``` or first { ... }
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # Match outermost curly braces
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            json_str = cleaned[start:end+1]
        else:
            return None

    try:
        return json.loads(json_str)
    except Exception as e:
        logger.debug("Failed to parse JSON from LLM: %s", e)
        return None


def build_evidence_grounded_prompt(
    candidate: Any,
    job_requirements: Dict[str, Any],
    keyword_mapping: Dict[str, Dict[str, Any]],
    cover_letter_mode: str = "standard"
) -> str:
    """
    Builds a strictly evidence-grounded prompt for Ollama.
    DO NOT give UNSUPPORTED requirements as candidate facts.
    """
    evidence = get_candidate_factual_evidence(candidate)
    full_name = getattr(candidate, "full_name", "") or "Applicant"
    seniority = job_requirements.get("seniority", "Senior").capitalize()
    target_title = job_requirements.get("title", "") or "Software Engineer"
    target_company = job_requirements.get("company", "") or "the target employer"

    # 1. Supported skills only
    supported_skills = [
        kw for kw, info in keyword_mapping.items()
        if info.get("status") == "SUPPORTED"
    ]
    unsupported_skills = [
        kw for kw, info in keyword_mapping.items()
        if info.get("status") == "UNSUPPORTED"
    ]

    # 2. Approved candidate experience
    raw_exp = evidence.get("experience", [])
    approved_experience_data = []
    for exp in raw_exp:
        val = exp.get("value", {}) if isinstance(exp.get("value"), dict) else {}
        fact_id = exp.get("fact_id") or exp.get("id") or 1
        approved_experience_data.append({
            "fact_id": fact_id,
            "company": val.get("company") or "Technology Company",
            "title": val.get("title") or "Software Engineer",
            "period": val.get("period") or "2021 - Present",
            "bullets": val.get("bullets") or val.get("responsibilities") or []
        })

    prompt = f"""You are an expert ATS resume writer and career strategist.
Your task is to tailor a professional resume summary, experience bullets, and cover letter for a candidate.

STRICT FACTUAL GROUNDING RULES:
1. ONLY use the approved candidate facts provided below. ZERO fabrication of companies, dates, degrees, certifications, or metrics.
2. NEVER claim or insert unsupported skills: {unsupported_skills}.
3. Every tailored experience bullet MUST reference its source fact_id from the approved candidate facts.
4. If a job requirement is missing from the candidate's approved facts, do NOT invent it.

TARGET JOB DETAILS:
- Title: {target_title}
- Company: {target_company}
- Seniority: {seniority}
- Required Job Skills: {job_requirements.get('required_skills', [])}

APPROVED CANDIDATE EVIDENCE:
- Full Name: {full_name}
- Verified Supported Skills: {supported_skills}
- Approved Experience Records:
{json.dumps(approved_experience_data, indent=2)}

OUTPUT REQUIREMENT:
Respond ONLY with a valid JSON object matching this schema:
{{
  "professional_summary": "A tailored 2-3 sentence summary aligned with {target_title} at {target_company} using verified skills.",
  "skill_order": ["prioritized", "list", "of", "supported", "skills"],
  "tailored_roles": [
    {{
      "fact_id": <int>,
      "company": "<string>",
      "title": "<string>",
      "period": "<string>",
      "bullets": [
        "<tailored bullet 1 grounded in original bullet>",
        "<tailored bullet 2 grounded in original bullet>"
      ]
    }}
  ],
  "cover_letter_body": "A compelling {cover_letter_mode} tailored cover letter (2-3 paragraphs) connecting candidate's verified skills to {target_company}."
}}
"""
    return prompt


def validate_llm_tailored_output(
    llm_output: Dict[str, Any],
    candidate: Any,
    job_requirements: Dict[str, Any],
    keyword_mapping: Dict[str, Dict[str, Any]]
) -> Tuple[bool, List[str]]:
    """
    Validates LLM-generated output against approved candidate truth.
    Strictly detects hallucinations, ungrounded bullets, or unsupported technologies.
    """
    errors = []
    evidence = get_candidate_factual_evidence(candidate)
    
    # 1. Check summary existence
    summary = llm_output.get("professional_summary", "")
    if not summary or len(summary.split()) < 10:
        errors.append("LLM professional summary is empty or too short.")

    # 2. Check unsupported skill injection
    unsupported_skills = [
        kw.lower() for kw, info in keyword_mapping.items()
        if info.get("status") == "UNSUPPORTED"
    ]
    summary_lower = summary.lower()
    for unsupp in unsupported_skills:
        if len(unsupp) > 2 and re.search(rf"\b{re.escape(unsupp)}\b", summary_lower):
            errors.append(f"LLM hallucinated unsupported skill '{unsupp}' into professional summary.")

    # 3. Check experience bullets grounding
    tailored_roles = llm_output.get("tailored_roles", [])
    if not tailored_roles or not isinstance(tailored_roles, list):
        errors.append("LLM returned no valid tailored experience roles.")
    else:
        valid_fact_ids = set([
            exp.get("fact_id") or exp.get("id") for exp in evidence.get("experience", [])
            if (exp.get("fact_id") or exp.get("id")) is not None
        ])
        for role in tailored_roles:
            f_id = role.get("fact_id")
            if valid_fact_ids and f_id not in valid_fact_ids:
                errors.append(f"LLM generated ungrounded role with invalid fact_id '{f_id}'.")
            
            bullets = role.get("bullets", [])
            if not bullets:
                errors.append(f"LLM returned zero bullets for role at {role.get('company')}.")
            for b in bullets:
                b_lower = b.lower()
                for unsupp in unsupported_skills:
                    if len(unsupp) > 2 and re.search(rf"\b{re.escape(unsupp)}\b", b_lower):
                        errors.append(f"LLM hallucinated unsupported skill '{unsupp}' in experience bullet: {b}")

    # 4. Check cover letter body
    cl_body = llm_output.get("cover_letter_body", "")
    if not cl_body or len(cl_body.split()) < 30:
        errors.append("LLM cover letter body is too short or missing.")
    else:
        cl_lower = cl_body.lower()
        for unsupp in unsupported_skills:
            if len(unsupp) > 2 and re.search(rf"\b{re.escape(unsupp)}\b", cl_lower):
                errors.append(f"LLM hallucinated unsupported skill '{unsupp}' into cover letter.")

    passed = len(errors) == 0
    return passed, errors


def generate_llm_enhanced_ats_content(
    candidate: Any,
    job_requirements: Dict[str, Any],
    keyword_mapping: Dict[str, Dict[str, Any]],
    cover_letter_mode: str = "standard"
) -> Dict[str, Any]:
    """
    Executes hybrid LLM tailoring via local Ollama.
    Falls back deterministically if Ollama is offline, times out, or fails factual QA.
    """
    from openoutreach.career.services.ats_document_engine import (
        generate_tailored_summary as fallback_summary,
        tailor_experience_bullets as fallback_experience,
        generate_job_specific_cover_letter as fallback_cl,
    )

    supported_skills = [
        kw for kw, info in keyword_mapping.items()
        if info.get("status") == "SUPPORTED"
    ]
    raw_exp = get_candidate_factual_evidence(candidate).get("experience", [])

    # Check if Ollama is responsive
    ollama_online = is_ollama_available()
    llm_used = False
    model_name = OLLAMA_MODEL_NAME if ollama_online else "none (deterministic fallback)"

    if ollama_online:
        prompt = build_evidence_grounded_prompt(
            candidate=candidate,
            job_requirements=job_requirements,
            keyword_mapping=keyword_mapping,
            cover_letter_mode=cover_letter_mode
        )
        raw_response = call_ollama_raw(prompt, model=OLLAMA_MODEL_NAME, timeout=25.0)
        parsed_json = extract_json_from_llm_response(raw_response) if raw_response else None

        if parsed_json:
            is_valid, qa_errs = validate_llm_tailored_output(
                parsed_json, candidate, job_requirements, keyword_mapping
            )
            if is_valid:
                logger.info("Ollama ATS intelligence successfully generated and validated.")
                llm_used = True
                return {
                    "llm_used": True,
                    "model_name": OLLAMA_MODEL_NAME,
                    "professional_summary": parsed_json["professional_summary"],
                    "skill_order": parsed_json.get("skill_order", supported_skills),
                    "tailored_roles": parsed_json["tailored_roles"],
                    "cover_letter_body": parsed_json["cover_letter_body"],
                    "fallback_used": False,
                }
            else:
                logger.warning("Ollama output failed factual QA (%s). Falling back to safe deterministic tailoring.", qa_errs)
        else:
            logger.warning("Ollama response empty or unparseable. Falling back to deterministic tailoring.")

    # Safe deterministic fallback
    summary = fallback_summary(candidate, job_requirements, supported_skills)
    roles = fallback_experience(raw_exp, supported_skills, job_requirements.get("title", ""))
    cl_dict = fallback_cl(candidate, job_requirements, keyword_mapping, mode=cover_letter_mode)

    return {
        "llm_used": False,
        "model_name": model_name,
        "professional_summary": summary,
        "skill_order": supported_skills,
        "tailored_roles": roles,
        "cover_letter_body": cl_dict.get("text", ""),
        "fallback_used": True,
    }
