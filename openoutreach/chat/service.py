# openoutreach/chat/service.py
"""Real Chat Job Application Onboarding & Questionnaire Service.

Orchestrates Ask-Once job application onboarding:
1. Detects job-application intent in user chat messages.
2. Checks CandidateProfile, active CandidateProfileVersion, CandidateApplicationProfile,
   and JobApplicationAnswer rows for existing confirmed facts.
3. Asks missing questions ONE AT A TIME through the real Chat interface.
4. Persists confirmed answers immediately with canonical keys.
5. Displays a full confirmation summary screen when all facts are present.
6. Reuses confirmed answers automatically without asking again on subsequent campaigns.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, Any, Tuple, Optional, List
from django.utils import timezone
from openoutreach.career.models import (
    CandidateProfile,
    CandidateApplicationProfile,
    JobApplicationAnswer,
)
from openoutreach.career.answer_bank import save_answer, get_answer_for_question, normalize_question

logger = logging.getLogger(__name__)

# Standard ordered sequence of onboarding questions
ONBOARDING_SEQUENCE = [
    ("phone", "What phone number should employers use?"),
    ("city_country", "What is your current city and country?"),
    ("work_authorization", "Where are you legally authorized to work? (e.g. US, UK, EU)"),
    ("visa_sponsorship", "Will you require visa sponsorship now or in the future?"),
    ("relocation", "Are you willing to relocate for a position?"),
    ("work_modes", "Which work modes are acceptable? (e.g. Remote, Hybrid, Onsite)"),
    ("target_roles", "What job titles or roles are you targeting?"),
    ("employment_types", "Which employment types do you prefer? (e.g. Full-time, Internship, Contract)"),
    ("earliest_start", "What is your earliest start date?"),
    ("notice_period", "What is your notice period?"),
    ("salary_expectation", "What is your expected salary range (USD or local currency)?"),
]


def process_chat_message(user, conv, content: str) -> str:
    """Process a user chat message within an active conversation."""
    content_clean = content.strip()
    content_lower = content_clean.lower()

    # 1. Check if user is confirming or answering an ongoing onboarding sequence
    if conv.onboarding_state == "JOB_ONBOARDING_IN_PROGRESS":
        return _handle_onboarding_answer(user, conv, content_clean)
    elif conv.onboarding_state == "JOB_ONBOARDING_REVIEW":
        return _handle_onboarding_confirmation(user, conv, content_lower)

    # 2. Check natural language commands
    from openoutreach.core.models import Campaign, WorkspaceMembership, WorkspaceAutomationSettings
    from openoutreach.crm.models import Deal, DealState
    from openoutreach.chat.models import CampaignBrief
    brief = None
    try:
        brief = conv.brief
    except Exception:
        brief = CampaignBrief.objects.filter(conversation=conv).first()

    ws_mem = WorkspaceMembership.objects.filter(user=user, status="active").first()
    ws = ws_mem.workspace if ws_mem else None

    if ws:
        active_camp = (brief.campaign if brief and brief.campaign else None) or Campaign.objects.filter(workspace=ws, is_active=True).last() or Campaign.objects.filter(workspace=ws).last()
    else:
        active_camp = (brief.campaign if brief and brief.campaign else None) or Campaign.objects.filter(users=user, is_active=True).last() or Campaign.objects.filter(users=user).last()

    auto_settings = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None

    # Pause Command
    if "pause" in content_lower or any(p in content_lower for p in ["stop outreach", "halt automation", "stop campaign"]):
        if active_camp:
            active_camp.is_active = False
            active_camp.save(update_fields=["is_active"])
        if ws:
            Campaign.objects.filter(workspace=ws, is_active=True).update(is_active=False)
        else:
            Campaign.objects.filter(users=user, is_active=True).update(is_active=False)
        if auto_settings:
            auto_settings.master_automation_enabled = False
            auto_settings.save(update_fields=["master_automation_enabled"])
        return "✓ Outreach automation has been safely paused. No further connection requests or messages will be sent until you resume."

    # Start / Resume Command
    if any(p in content_lower for p in ["start outreach", "start campaign", "start automation", "resume outreach", "resume campaign", "resume", "unpause", "launch automation"]) or content_lower == "start":
        if active_camp:
            active_camp.is_active = True
            active_camp.save(update_fields=["is_active"])
        if ws:
            Campaign.objects.filter(workspace=ws, is_synthetic=False).update(is_active=True)
        else:
            Campaign.objects.filter(users=user, is_synthetic=False).update(is_active=True)
        if auto_settings:
            auto_settings.master_automation_enabled = True
            auto_settings.save(update_fields=["master_automation_enabled"])
        return "✓ Outreach automation is now active! Background workers will process scheduled networking and job actions."

    # Daily Limit Command ("Send max 10 connections per day")
    limit_match = re.search(r'(?:max|limit(?: to)?|send)\s*(\d+)\s*(?:connections?|requests?|per day|daily)', content_lower)
    if limit_match and active_camp:
        limit_num = int(limit_match.group(1))
        active_camp.daily_connect_limit = limit_num
        active_camp.save(update_fields=["daily_connect_limit"])
        return f"✓ Updated Campaign '{active_camp.name}' daily limit to {limit_num} connection requests per day."

    # Follow-up Command ("Follow up when they accept")
    if "follow up" in content_lower and ("accept" in content_lower or "enable" in content_lower):
        if active_camp:
            active_camp.enable_auto_followup = True
            active_camp.save(update_fields=["enable_auto_followup"])
        return "✓ Automatic follow-up enabled! A personalized message will be sent when a prospect accepts your connection request."

    # Show Replies Command ("Show people who replied")
    if any(p in content_lower for p in ["who replied", "show replies", "recent replies", "check replies"]):
        replies = Deal.objects.filter(state__in=[DealState.CONNECTED, DealState.COMPLETED]).order_by("-update_date")[:5]
        if replies.exists():
            summary_lines = [f"• **{r.lead.public_identifier}** ({r.campaign.name}) - Status: {r.get_state_display()}" for r in replies]
            return "Here are recent replies from your network:\n\n" + "\n".join(summary_lines)
        return "No new prospect replies found in your inbox yet. As soon as someone replies, you'll see them in your Inbox."

    # Targeting Command ("Find startup founders in Dubai", "Target Python developers in Berlin")
    target_match = re.search(r'(?:find|target|search for)\s+([^,]+?)\s+in\s+([^,\.\?!]+)', content_clean, re.IGNORECASE)
    if target_match and active_camp:
        role_kw = target_match.group(1).strip()
        geo = target_match.group(2).strip()
        active_camp.target_audience_definition = f"Target Roles: {role_kw} | Location: {geo}"
        active_camp.save(update_fields=["target_audience_definition"])
        return f"✓ Updated targeting for Campaign '{active_camp.name}':\n• **Target Roles**: {role_kw}\n• **Location**: {geo}\n\nHuntFlow will find prospects matching these criteria."

    # 3. Intent detection for job applications
    if any(k in content_lower for k in ["job", "internship", "remote job", "python job", "apply for jobs"]):
        cand_prof = CandidateProfile.objects.filter(user=user).first()
        if not cand_prof:
            return "No CandidateProfile found for your account. Please set up your profile first under Settings."

        app_prof, _ = CandidateApplicationProfile.objects.get_or_create(candidate_profile=cand_prof)
        
        # Check if already fully confirmed
        if app_prof.confirmed:
            conv.onboarding_state = "JOB_ONBOARDING_CONFIRMED"
            conv.save(update_fields=["onboarding_state"])
            return _generate_campaign_launch_summary(app_prof)

        # Start onboarding questionnaire for missing facts
        conv.onboarding_state = "JOB_ONBOARDING_IN_PROGRESS"
        conv.onboarding_data = {"current_index": 0}
        conv.save(update_fields=["onboarding_state", "onboarding_data"])

        first_q = _get_next_unanswered_question(cand_prof, app_prof, 0, conv)
        if first_q:
            return f"I can set up a Job Application Campaign for you!\n\nBefore I start, I need a few details employers commonly ask.\n\n{first_q}"
        else:
            conv.onboarding_state = "JOB_ONBOARDING_REVIEW"
            conv.save(update_fields=["onboarding_state"])
            return _generate_profile_review_summary(app_prof)

    # Fallback to LLM assistant generation if configured
    from openoutreach.core.models import SiteConfig
    cfg = SiteConfig.load()
    if cfg.is_llm_configured():
        try:
            from openoutreach.career.services.ats_llm_engine import call_ollama_raw, is_ollama_available
            if is_ollama_available():
                system_prompt = (
                    "You are HuntFlow AI Assistant. You help the user manage outreach campaigns, "
                    "write high-converting copy, review targeting, and optimize lead generation. "
                    "Keep responses concise and helpful."
                )
                prompt = f"{system_prompt}\n\nUser Question: {content_clean}\n\nAnswer:"
                timeout_sec = float(os.environ.get("OLLAMA_REQUEST_TIMEOUT", "60.0"))
                ans = call_ollama_raw(prompt, timeout=timeout_sec)
                if ans and ans.strip():
                    return ans.strip()
        except Exception as e:
            logger.warning("Ollama chat inference failed: %s", e)

        try:
            from openoutreach.core.llm import get_llm_model, run_agent_sync
            from pydantic_ai import Agent
            model = get_llm_model()
            agent = Agent(model, system_prompt="You are HuntFlow AI Assistant. Answer concisely.")
            res = run_agent_sync(agent.run(content_clean))
            out_val = getattr(res, "output", getattr(res, "data", None))
            if out_val:
                return str(out_val)
        except Exception as e:
            logger.warning("LLM chat inference failed: %s", e)

    return f"I've noted: '{content_clean}'. I can help you configure your campaigns, change limits, search for roles, or manage automation settings in plain English."


def _get_next_unanswered_question(cand_prof, app_prof, start_idx: int, conv) -> Optional[str]:
    """Find the next unanswered question in the onboarding sequence."""
    for idx in range(start_idx, len(ONBOARDING_SEQUENCE)):
        key, q_text = ONBOARDING_SEQUENCE[idx]
        if not _is_fact_known(cand_prof, app_prof, key):
            conv.onboarding_data["current_index"] = idx
            conv.onboarding_data["current_key"] = key
            conv.save(update_fields=["onboarding_data"])
            return q_text
    return None


def _is_fact_known(cand_prof, app_prof, key: str) -> bool:
    """Check if a specific onboarding fact is already confirmed in the answer bank or profile."""
    if key == "phone":
        return bool(app_prof.phone or cand_prof.phone)
    elif key == "city_country":
        return bool(app_prof.city and app_prof.country)
    elif key == "work_authorization":
        return bool(app_prof.authorized_countries)
    elif key == "visa_sponsorship":
        return bool(app_prof.requires_sponsorship_now or app_prof.requires_sponsorship_future)
    elif key == "relocation":
        return bool(app_prof.willing_to_relocate is not None and app_prof.confirmed)
    elif key == "work_modes":
        return bool(app_prof.work_modes)
    elif key == "target_roles":
        return bool(app_prof.target_job_titles)
    elif key == "employment_types":
        return bool(app_prof.employment_types)
    elif key == "earliest_start":
        return bool(app_prof.earliest_start_date)
    elif key == "notice_period":
        return bool(app_prof.notice_period)
    elif key == "salary_expectation":
        return bool(app_prof.minimum_salary)
    return False


def _handle_onboarding_answer(user, conv, answer_text: str) -> str:
    """Save an incoming user answer and move to the next question."""
    cand_prof = CandidateProfile.objects.get(user=user)
    app_prof, _ = CandidateApplicationProfile.objects.get_or_create(candidate_profile=cand_prof)
    
    current_key = conv.onboarding_data.get("current_key")
    current_idx = conv.onboarding_data.get("current_index", 0)

    # Normalize & persist answer to CandidateApplicationProfile and JobApplicationAnswer bank
    if current_key == "phone":
        app_prof.phone = answer_text
        save_answer(cand_prof, "phone_number", answer_text, answer_type="text")
    elif current_key == "city_country":
        parts = [p.strip() for p in answer_text.split(",")]
        app_prof.city = parts[0]
        app_prof.country = parts[1] if len(parts) > 1 else "US"
        save_answer(cand_prof, "location_city", app_prof.city)
        save_answer(cand_prof, "location_country", app_prof.country)
    elif current_key == "work_authorization":
        auths = [a.strip().upper() for a in answer_text.split(",")]
        app_prof.authorized_countries = auths
        save_answer(cand_prof, "work_authorization_countries", auths, answer_type="multi_choice")
    elif current_key == "visa_sponsorship":
        requires = "yes" in answer_text.lower() or "require" in answer_text.lower()
        app_prof.requires_sponsorship_now = requires
        app_prof.requires_sponsorship_future = requires
        save_answer(cand_prof, "requires_sponsorship_now", requires, answer_type="boolean")
    elif current_key == "relocation":
        app_prof.willing_to_relocate = "yes" in answer_text.lower()
        save_answer(cand_prof, "willing_to_relocate", app_prof.willing_to_relocate, answer_type="boolean")
    elif current_key == "work_modes":
        modes = [m.strip().capitalize() for m in answer_text.split(",")]
        app_prof.work_modes = modes
        save_answer(cand_prof, "work_modes", modes, answer_type="multi_choice")
    elif current_key == "target_roles":
        roles = [r.strip() for r in answer_text.split(",")]
        app_prof.target_job_titles = roles
        save_answer(cand_prof, "target_job_titles", roles, answer_type="multi_choice")
    elif current_key == "employment_types":
        types = [t.strip().capitalize() for t in answer_text.split(",")]
        app_prof.employment_types = types
        save_answer(cand_prof, "employment_types", types, answer_type="multi_choice")
    elif current_key == "earliest_start":
        app_prof.earliest_start_date = answer_text
        save_answer(cand_prof, "earliest_start_date", answer_text, answer_type="text")
    elif current_key == "notice_period":
        app_prof.notice_period = answer_text
        save_answer(cand_prof, "notice_period", answer_text, answer_type="text")
    elif current_key == "salary_expectation":
        import re
        nums = re.findall(r"\d+", answer_text.replace(",", ""))
        if nums:
            app_prof.minimum_salary = int(nums[0])
            save_answer(cand_prof, "salary_expectation", app_prof.minimum_salary, answer_type="number")

    app_prof.save()

    # Move to next question
    next_q = _get_next_unanswered_question(cand_prof, app_prof, current_idx + 1, conv)
    if next_q:
        return f"Got it, saved!\n\n{next_q}"
    else:
        conv.onboarding_state = "JOB_ONBOARDING_REVIEW"
        conv.save(update_fields=["onboarding_state"])
        return _generate_profile_review_summary(app_prof)


def _handle_onboarding_confirmation(user, conv, content_lower: str) -> str:
    """Handle confirmation step after review screen."""
    cand_prof = CandidateProfile.objects.get(user=user)
    app_prof = CandidateApplicationProfile.objects.get(candidate_profile=cand_prof)

    if any(word in content_lower for word in ["yes", "use", "automatically", "confirm", "ok", "start"]):
        app_prof.confirmed = True
        app_prof.confirmed_by = user
        app_prof.confirmed_at = timezone.now()
        app_prof.save()

        conv.onboarding_state = "JOB_ONBOARDING_CONFIRMED"
        conv.save(update_fields=["onboarding_state"])
        return _generate_campaign_launch_summary(app_prof)
    else:
        return "You can edit your saved answers anytime under Settings → Job Application Profile. Please type 'Use automatically' when you're ready to proceed."


def _generate_profile_review_summary(app_prof: CandidateApplicationProfile) -> str:
    """Generate markdown summary screen for final user confirmation."""
    return (
        "### 📋 Job Application Profile Summary\n\n"
        f"• **Name:** {app_prof.full_name or app_prof.candidate_profile.full_name}\n"
        f"• **Email:** {app_prof.email or app_prof.candidate_profile.email}\n"
        f"• **Phone:** {app_prof.phone}\n"
        f"• **Location:** {app_prof.city}, {app_prof.country}\n"
        f"• **Work Authorization:** {', '.join(app_prof.authorized_countries) if app_prof.authorized_countries else 'None'}\n"
        f"• **Visa Sponsorship Required:** {'Yes' if app_prof.requires_sponsorship_now else 'No'}\n"
        f"• **Willing to Relocate:** {'Yes' if app_prof.willing_to_relocate else 'No'}\n"
        f"• **Work Modes:** {', '.join(app_prof.work_modes) if app_prof.work_modes else 'Any'}\n"
        f"• **Target Roles:** {', '.join(app_prof.target_job_titles) if app_prof.target_job_titles else 'Software Engineer'}\n"
        f"• **Employment Types:** {', '.join(app_prof.employment_types) if app_prof.employment_types else 'Full-time'}\n"
        f"• **Earliest Start:** {app_prof.earliest_start_date}\n"
        f"• **Notice Period:** {app_prof.notice_period}\n"
        f"• **Salary Expectation:** ${app_prof.minimum_salary:,} USD/yr\n\n"
        "**Should I use these answers automatically for future job applications?**\n\n"
        "(Type **'Use automatically'** to confirm and start)"
    )


def _generate_campaign_launch_summary(app_prof: CandidateApplicationProfile) -> str:
    """Generate final campaign launch message."""
    return (
        "🚀 **Job Application Campaign Active!**\n\n"
        f"• Candidate: {app_prof.full_name or app_prof.candidate_profile.full_name}\n"
        f"• Status: Answers confirmed & saved globally\n"
        f"• Pacing: 1 application / 24 hrs\n"
        f"• Gating: Missing/unknown screening questions will prompt you in chat before submission.\n\n"
        "I will now scan open positions and match them against your confirmed profile!"
    )
