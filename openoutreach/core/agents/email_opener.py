# openoutreach/core/agents/email_opener.py
"""Email opener agent: composes the single Layer-1 cold email for a deal.

A distinct entrypoint from the follow-up agent. Layer 1 email is one outbound
touch — no thread to read, no send/wait/give-up decision — so the structured
output is just a subject + body. The prompt shares the outreach base (identity,
product docs, lead summary, Mom Test strategy, language/no-placeholder rules) via
``email_opener.j2`` and only adds the cold-email framing + the subject request.
The multi-turn email conversation is the hosted Layer-2 backend's job.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from openoutreach.core.agents.prompt import base_context, render
from openoutreach.core.llm import get_llm_model, run_agent_sync

logger = logging.getLogger(__name__)


class EmailDraft(BaseModel):
    """Structured output from the email opener agent."""

    subject: str = Field(description="The email subject line — short, specific, like a real person wrote it; not salesy.")
    body: str = Field(description="The email body. A few short sentences; no signature, no placeholders.")


def compose_opener_email(session, deal) -> EmailDraft:
    """Compose the opener subject + body for ``deal`` from its summaries + campaign docs."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    system_prompt = render("email_opener.j2", **base_context(session, deal))

    try:
        agent = Agent(
            get_llm_model(),
            output_type=EmailDraft,
            model_settings={"temperature": 0.7, "timeout": 60},
        )
        draft = run_agent_sync(agent.run(system_prompt)).output
        if draft:
            return draft
    except Exception as exc:
        logger.warning(f"Structured EmailDraft output failed ({exc}) — attempting string parsing fallback")

    # String fallback for local LLMs
    str_agent = Agent(
        get_llm_model(),
        system_prompt=system_prompt + "\n\nFormat output strictly as:\nSubject: <subject>\nBody:\n<body>",
        model_settings={"temperature": 0.7, "timeout": 60},
    )
    res = run_agent_sync(str_agent.run("Compose cold email draft."))
    text = str(res.output) if res and res.output else ""
    subject = "Quick question"
    body = text.strip()

    if "Subject:" in text:
        parts = text.split("Subject:", 1)[1]
        if "Body:" in parts:
            subj_part, body_part = parts.split("Body:", 1)
            subject = subj_part.strip()
            body = body_part.strip()
        else:
            lines = parts.strip().split("\n", 1)
            subject = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else body

    return EmailDraft(subject=subject, body=body)
