import hashlib
from abc import ABC, abstractmethod
from typing import Tuple, Dict, Any, List
from django.utils import timezone
from django.core.exceptions import ValidationError
from openoutreach.career.models import (
    CandidateProfileVersion,
    Job,
    JobApplication,
    ApplicationDraft
)
from openoutreach.core.llm import get_llm_model, run_agent_sync
from pydantic import BaseModel, Field
from pydantic_ai import Agent

class TailoredApplicationResult(BaseModel):
    tailored_cv: str = Field(description="Markdown format of the tailored CV, emphasizing elements relevant to the job, strictly grounded in candidate facts.")
    cover_letter: str = Field(description="Tailored cover letter targeting the job description, strictly grounded in candidate facts.")

class BaseDraftingProvider(ABC):
    @abstractmethod
    def generate_draft(self, profile_version: CandidateProfileVersion, job: Job) -> Tuple[str, str, Dict[str, Any], Dict[str, Any]]:
        """Generate tailored CV markdown, cover letter, structured sections, and factual traceability map.
        
        Returns:
            Tuple of (tailored_cv, cover_letter, structured_cv, traceability_map)
        """
        pass

class OfflineDraftingProvider(BaseDraftingProvider):
    """Deterministic offline drafting provider.
    Ensures 100% factual grounding by directly formatting candidate facts without external LLM calls.
    """
    def generate_draft(self, profile_version: CandidateProfileVersion, job: Job) -> Tuple[str, str, Dict[str, Any], Dict[str, Any]]:
        # 1. Build structured CV and traceability map
        structured_cv = {
            "summary": profile_version.summary or f"Experienced professional targeting {job.title} at {job.company}.",
            "experience": [],
            "education": [],
            "skills": [],
            "certifications": []
        }
        
        traceability_map = {
            "summary_source": "profile_version.summary",
            "experience_mappings": [],
            "education_mappings": [],
            "skill_mappings": [],
            "certification_mappings": []
        }
        
        # Format markdown blocks
        cv_lines = []
        cv_lines.append(f"# {profile_version.full_name}")
        cv_lines.append(f"Email: {profile_version.email} | Phone: {profile_version.phone}")
        if profile_version.linkedin_url:
            cv_lines.append(f"LinkedIn: {profile_version.linkedin_url}")
        cv_lines.append("\n## Professional Summary")
        cv_lines.append(structured_cv["summary"])
        
        experience_lines = ["\n## Work Experience"]
        education_lines = ["\n## Education"]
        skills_lines = ["\n## Skills"]
        certs_lines = ["\n## Certifications"]
        
        for fact in profile_version.facts.all():
            if fact.fact_type == "experience":
                comp = fact.value.get("company", "")
                title = fact.value.get("title", "")
                start = fact.value.get("start", "")
                end = fact.value.get("end", "Present")
                bullets = fact.value.get("bullet_points", [])
                
                exp_item = {
                    "company": comp,
                    "title": title,
                    "start": start,
                    "end": end,
                    "bullet_points": bullets
                }
                structured_cv["experience"].append(exp_item)
                
                # Traceability link
                traceability_map["experience_mappings"].append({
                    "company": comp,
                    "title": title,
                    "fact_id": fact.id
                })
                
                experience_lines.append(f"### {title} at {comp} ({start} - {end})")
                for b in bullets:
                    experience_lines.append(f"- {b}")
                    
            elif fact.fact_type == "education":
                school = fact.value.get("school", "")
                degree = fact.value.get("degree", "")
                year = fact.value.get("year", "")
                
                edu_item = {
                    "school": school,
                    "degree": degree,
                    "year": year
                }
                structured_cv["education"].append(edu_item)
                
                traceability_map["education_mappings"].append({
                    "school": school,
                    "degree": degree,
                    "fact_id": fact.id
                })
                education_lines.append(f"- {degree}, {school} ({year})")
                
            elif fact.fact_type == "skill":
                name = fact.value.get("name", "")
                structured_cv["skills"].append(name)
                traceability_map["skill_mappings"].append({
                    "name": name,
                    "fact_id": fact.id
                })
                
            elif fact.fact_type == "certification":
                name = fact.value.get("name", "")
                issuer = fact.value.get("issuer", "")
                year = fact.value.get("year", "")
                
                cert_item = {
                    "name": name,
                    "issuer": issuer,
                    "year": year
                }
                structured_cv["certifications"].append(cert_item)
                traceability_map["certification_mappings"].append({
                    "name": name,
                    "fact_id": fact.id
                })
                certs_lines.append(f"- {name} by {issuer} ({year})")
        
        # Build skills line
        if structured_cv["skills"]:
            skills_lines.append(", ".join(structured_cv["skills"]))
            
        # Join CV blocks
        cv_lines.extend(experience_lines)
        cv_lines.extend(education_lines)
        cv_lines.extend(skills_lines)
        if structured_cv["certifications"]:
            cv_lines.extend(certs_lines)
            
        tailored_cv = "\n".join(cv_lines)
        
        # Generate cover letter deterministically
        cl_parts = [
            f"Dear Hiring Team at {job.company},\n",
            f"I am writing to express my strong interest in the {job.title} position at {job.company}.\n",
            f"Based on my career summary as a {profile_version.summary or 'professional'}, I believe I am well-suited for this challenge.",
        ]
        if structured_cv["skills"]:
            cl_parts.append(f"My key strengths include: {', '.join(structured_cv['skills'][:5])}.")
        cl_parts.append("\nThank you for your consideration.\n\nSincerely,")
        cl_parts.append(profile_version.full_name)
        cover_letter = "\n".join(cl_parts)
        
        return tailored_cv, cover_letter, structured_cv, traceability_map

class AIDraftingProvider(BaseDraftingProvider):
    """Pydantic-AI based online drafting provider."""
    def generate_draft(self, profile_version: CandidateProfileVersion, job: Job) -> Tuple[str, str, Dict[str, Any], Dict[str, Any]]:
        from openoutreach.career.service import build_candidate_profile_text
        profile_text = build_candidate_profile_text(profile_version)
        job_desc = job.description
        
        model = get_llm_model()
        agent = Agent(
            model,
            output_type=TailoredApplicationResult,
            system_prompt=(
                "You are a professional resume writer and career assistant.\n"
                "Generate a tailored CV (in Markdown format) and a cover letter targeting the job description.\n"
                "STRICT GROUNDING RULES:\n"
                "- Do NOT invent, assume, or extrapolate any facts, dates, companies, locations, metrics, or certifications.\n"
                "- Any skills, experiences, achievements, or certifications mentioned in the tailored CV or cover letter MUST exist explicitly in the Candidate Profile Info.\n"
                "- If a job requirement is not supported by the candidate's profile facts, do NOT make up any experience for it. You may highlight relevant skills the candidate actually has, but you must never hallucinate.\n"
                "- If you cannot find any matching experience for a requirement, omit it or flag it as not present rather than generating unsupported claims."
            )
        )
        prompt = (
            f"Candidate Profile Info:\n{profile_text}\n\n"
            f"Target Job Description:\n{job_desc}"
        )
        result = run_agent_sync(agent.run(prompt))
        output = result.output
        
        structured_cv = {
            "summary": profile_version.summary,
            "experience": [],
            "education": [],
            "skills": [],
            "certifications": []
        }
        traceability_map = {
            "summary_source": "profile_version.summary",
            "experience_mappings": [],
            "education_mappings": [],
            "skill_mappings": [],
            "certification_mappings": []
        }
        
        for fact in profile_version.facts.all():
            if fact.fact_type == "experience":
                structured_cv["experience"].append(fact.value)
                traceability_map["experience_mappings"].append({
                    "company": fact.value.get("company"),
                    "title": fact.value.get("title"),
                    "fact_id": fact.id
                })
            elif fact.fact_type == "education":
                structured_cv["education"].append(fact.value)
                traceability_map["education_mappings"].append({
                    "school": fact.value.get("school"),
                    "degree": fact.value.get("degree"),
                    "fact_id": fact.id
                })
            elif fact.fact_type == "skill":
                name = fact.value.get("name")
                structured_cv["skills"].append(name)
                traceability_map["skill_mappings"].append({
                    "name": name,
                    "fact_id": fact.id
                })
            elif fact.fact_type == "certification":
                structured_cv["certifications"].append(fact.value)
                traceability_map["certification_mappings"].append({
                    "name": fact.value.get("name"),
                    "fact_id": fact.id
                })
                
        return output.tailored_cv, output.cover_letter, structured_cv, traceability_map

def get_drafting_provider(offline: bool = True) -> BaseDraftingProvider:
    if offline:
        return OfflineDraftingProvider()
    return AIDraftingProvider()
