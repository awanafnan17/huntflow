# tests/test_candidate_voice_fidelity.py
"""Tests for Candidate's authentic writing style, corporate cliché rejection, and voice fidelity."""
from __future__ import annotations

import pytest
from openoutreach.core.content_style import (
    validate_candidate_message_style,
    validate_user_facing_content_style,
    CANDIDATE_LINKEDIN_CORPUS,
)

# Negative regression fixture: The real production failure reported by operator (Sam Davies message)
REJECTED_SCREENSHOT_EXAMPLE = (
    "Hello, I'm completing degrees in Geology and Computer Science and "
    "building GeoAI tools for exploration screening. Your experience in "
    "mineral exploration and connection with UWA are highly relevant to my "
    "goals. I’d be glad to connect and follow your insights."
)

# Negative regression fixture 2: The historical Ashot corporate message
ASHOT_WRONG_STYLE_EXAMPLE = (
    "Hi Ashot, I saw you work on AI solutions at PMI. I'm building ML-driven products "
    "including a geospatial analytics startup and recently competed in physics-informed "
    "ML challenges. Would be great to connect and exchange insights on applied AI work."
)


def test_sam_davies_screenshot_message_fails():
    """Verify that the rejected formal message fails Candidate style validation with specific error codes."""
    res = validate_candidate_message_style(REJECTED_SCREENSHOT_EXAMPLE, content_type="connection_note")
    assert not res["valid"], "Sam Davies formal message must fail style validation!"
    
    error_codes = res["error_codes"]
    assert "FORMAL_OPENING" in error_codes, f"Expected FORMAL_OPENING in {error_codes}"
    assert "RESUME_DUMP" in error_codes, f"Expected RESUME_DUMP in {error_codes}"
    assert "CORPORATE_LANGUAGE" in error_codes, f"Expected CORPORATE_LANGUAGE in {error_codes}"
    assert "TOO_MUCH_CONTEXT" in error_codes, f"Expected TOO_MUCH_CONTEXT in {error_codes}"


def test_good_shadow_rewrite_passes():
    """Verify that the authentic Candidate shadow rewrite for Sam Davies passes validation."""
    good_rewrite = (
        "saw the exploration + UWA background... pretty close to the direction "
        "i'm getting into rn. curious what kind of exploration work you're doing these days?"
    )
    res = validate_candidate_message_style(good_rewrite, content_type="connection_note")
    assert res["valid"], f"Good shadow rewrite should pass! Errors: {res.get('errors')}"
    
    metrics = res["style_metrics"]
    assert metrics["starts_lowercase"] is True
    assert metrics["has_ellipsis"] is True
    assert metrics["has_plus_sign"] is True
    assert metrics["has_slang"] is True
    assert metrics["question_count"] == 1
    assert metrics["is_chat_like"] is True


def test_ashot_negative_regression_fixture_fails():
    """Verify that the corporate AI message sent to Ashot explicitly fails Candidate style validation."""
    res = validate_candidate_message_style(ASHOT_WRONG_STYLE_EXAMPLE, content_type="connection_note")
    assert not res["valid"], "Ashot corporate message should have failed style validation!"
    
    error_codes = res["error_codes"]
    assert "CORPORATE_LANGUAGE" in error_codes or "RESUME_DUMP" in error_codes


def test_corporate_cliches_fail():
    """Verify standard corporate sales copy fails validation."""
    bad_messages = [
        "I would love to connect and explore synergies with your team.",
        "I came across your impressive profile and wanted to connect with like-minded professionals.",
        "Your remarkable experience caught my attention. I am reaching out because we build AI tools.",
        "I noticed your impressive background in fintech. I'd be delighted to connect.",
        "I wanted to follow up regarding my previous correspondence.",
        "Your extensive professional experience is highly relevant to my goals.",
        "I hope this message finds you well.",
        "I'd be glad to connect and follow your insights.",
        "We can leverage my expertise to create mutually beneficial solutions.",
    ]
    for msg in bad_messages:
        res = validate_candidate_message_style(msg, content_type="connection_note")
        assert not res["valid"], f"Message should have failed corporate check: {msg}"
        assert "CORPORATE_LANGUAGE" in res["error_codes"]


def test_resume_dumping_fails():
    """Verify dumping multiple credentials/startups into an opening message fails."""
    bad_messages = [
        "I'm building a geospatial analytics startup and recently competed in physics challenges.",
        "Founder of GeoSentry startup, degree in physics, and competed in ML challenges.",
        "Hello, I am completing degrees in Geology and Computer Science.",
        "I have a background in Computer Science and Geology.",
        "My expertise includes machine learning, geospatial analysis, and cloud infrastructure.",
    ]
    for msg in bad_messages:
        res = validate_candidate_message_style(msg, content_type="connection_note")
        assert not res["valid"], f"Resume dump should have failed: {msg}"
        assert "RESUME_DUMP" in res["error_codes"] or "FORMAL_OPENING" in res["error_codes"]


def test_formal_closings_fail():
    """Verify formal closings and sign-offs are rejected."""
    bad_messages = [
        "hey Alex, saw your work on indexing... curious what you're focused on? Best regards, Candidate",
        "saw what you guys are building at DeepScale... Sincerely, Candidate",
        "noticed your work in automation... Look forward to connecting.",
    ]
    for msg in bad_messages:
        res = validate_candidate_message_style(msg, content_type="connection_note")
        assert not res["valid"], f"Formal closing should have failed: {msg}"


def test_candidate_corpus_positive_examples_pass():
    """Verify all curated authentic Candidate corpus messages pass style validation."""
    for i, item in enumerate(CANDIDATE_LINKEDIN_CORPUS):
        text = item["text"]
        tag = item["tag"]
        content_type = "connection_note" if tag == "CONNECTION" else "follow_up"
        res = validate_candidate_message_style(text, content_type=content_type)
        assert res["valid"], f"Corpus example {i+1} [{tag}] failed: {text} | Errors: {res.get('errors')}"


def test_better_direction_ashot_alternative_passes():
    """Verify the approved conversational alternative for Ashot passes validation."""
    better_msg = (
        "saw you're working on product AI at PMI... i'm building around AI/automation "
        "too so the overlap caught my eye. curious what kind of problems you're focused on there?"
    )
    res = validate_candidate_message_style(better_msg, content_type="connection_note")
    assert res["valid"], f"Better direction message should pass, got errors: {res.get('errors')}"
    assert len(better_msg) <= 300
    assert better_msg.count("?") <= 1


def test_vp_marketing_product_infrastructure_assumption_fails():
    """VP of Marketing must NOT be assumed to work on product infrastructure without evidence."""
    from openoutreach.core.content_style import validate_generated_content_claims
    
    context = {"title": "VP of Marketing", "company": "Bitwage"}
    bad_msg = "saw you're working on product infrastructure at Bitwage... curious what kind of problems you're focused on there?"
    res = validate_generated_content_claims(bad_msg, context=context)
    assert not res["valid"], "Non-tech marketing role should fail product infrastructure claim!"
    assert any("product infrastructure" in err for err in res["errors"])


def test_engineer_leading_engineering_assumption_fails():
    """Individual contributor Engineer must NOT be assumed to lead engineering without evidence."""
    from openoutreach.core.content_style import validate_generated_content_claims
    
    context = {"title": "Software Engineer", "company": "Tech Corp"}
    bad_msg = "hey Alex, saw you're leading engineering at Tech Corp... how do you guys handle worker failovers?"
    res = validate_generated_content_claims(bad_msg, context=context)
    assert not res["valid"], "Individual contributor engineer should fail leading engineering claim!"
    assert any("leading engineering" in err for err in res["errors"])


def test_grounded_message_passes():
    """Well-grounded role-specific messages pass truth validation."""
    from openoutreach.core.content_style import validate_generated_content_claims
    
    context_mkt = {"title": "VP of Marketing", "company": "Bitwage"}
    valid_mkt = "hey Scott, saw your marketing work at Bitwage... curious what part of client acquisition has been trickiest to scale?"
    res_mkt = validate_generated_content_claims(valid_mkt, context=context_mkt)
    assert res_mkt["valid"], f"Grounded marketing message failed: {res_mkt['errors']}"

    context_eng = {"title": "Lead Engineering Architect", "company": "Tech Corp"}
    valid_eng = "hey Samantha, saw you're leading engineering architecture at Tech Corp... how do you guys handle worker failovers?"
    res_eng = validate_generated_content_claims(valid_eng, context=context_eng)
    assert res_eng["valid"], f"Grounded tech lead message failed: {res_eng['errors']}"


def test_student_ai_claim_fails():
    """Student with no AI evidence must fail AI/automation assertions."""
    from openoutreach.core.content_style import validate_generated_content_claims
    context = {"title": "Student", "company": "Student"}
    msg = "hey sania, saw your work on ai/automation. curious if you're working on any specific projects?"
    res = validate_generated_content_claims(msg, context=context)
    assert not res["valid"]
    assert any("ai/automation" in err.lower() for err in res["errors"])


def test_ecommerce_python_architecture_premise_fails():
    """E-commerce manager must fail Python/backend architecture claims."""
    from openoutreach.core.content_style import validate_generated_content_claims
    context = {"title": "E-commerce Manager", "company": "Prusa Research"}
    msg = "hey tomáš, saw you're scaling d2c at prusa research. curious about backend python/architecture challenges?"
    res = validate_generated_content_claims(msg, context=context)
    assert not res["valid"]
    assert any("python/backend" in err.lower() for err in res["errors"])


def test_empty_message_fails_style_and_claims():
    """Empty or whitespace messages must explicitly fail validation and never be marked valid."""
    from openoutreach.core.content_style import validate_candidate_message_style, validate_generated_content_claims
    
    for empty_val in ["", "   ", "\n\t", None]:
        style_res = validate_candidate_message_style(empty_val)
        claims_res = validate_generated_content_claims(empty_val)
        assert not style_res["valid"], f"Empty message should fail style: {empty_val!r}"
        assert not claims_res["valid"], f"Empty message should fail claims: {empty_val!r}"


def test_question_limit_exceeded_fails():
    """Multiple questions in an opener must fail QUESTION_LIMIT_EXCEEDED."""
    multi_q = "hey mark, saw your work at acme... how is the stack holding up? are you guys hiring rn?"
    res = validate_candidate_message_style(multi_q, content_type="connection_note")
    assert not res["valid"]
    assert "QUESTION_LIMIT_EXCEEDED" in res["error_codes"]


def test_validate_linkedin_outbound_message_gate():
    """Verify validate_linkedin_outbound_message handles style, claims, and identity checks."""
    from openoutreach.core.content_style import validate_linkedin_outbound_message

    # 1. Valid message
    valid_text = "hey mark, saw your work on exploration screening... curious what you're focused on rn?"
    res = validate_linkedin_outbound_message(valid_text, "connection_note")
    assert res["allowed"] is True
    assert res["block_reason"] is None
    assert res["style_result"]["valid"] is True

    # 2. Formal screenshot failure
    res_bad = validate_linkedin_outbound_message(REJECTED_SCREENSHOT_EXAMPLE, "connection_note")
    assert res_bad["allowed"] is False
    assert res_bad["block_reason"] == "MESSAGE_BLOCKED_STYLE"
    assert "FORMAL_OPENING" in res_bad["style_result"]["error_codes"]

    # 3. Incident hold
    res_hold = validate_linkedin_outbound_message(valid_text, "connection_note", context={"incident_hold_active": True})
    assert res_hold["allowed"] is False
    assert res_hold["block_reason"] == "MESSAGE_BLOCKED_SAFETY_HOLD"

    # 4. Unverified identity
    res_unverified = validate_linkedin_outbound_message(valid_text, "connection_note", context={"identity_unverified": True})
    assert res_unverified["allowed"] is False
    assert res_unverified["block_reason"] == "MESSAGE_BLOCKED_UNVERIFIED_IDENTITY"


def test_prohibited_punctuation_fails():
    """Em-dashes and double hyphens fail with PROHIBITED_PUNCTUATION."""
    msg1 = "hey sam, saw your work — pretty cool tech."
    res1 = validate_candidate_message_style(msg1, content_type="connection_note")
    assert not res1["valid"]
    assert "PROHIBITED_PUNCTUATION" in res1["error_codes"]

    msg2 = "hey sam, saw your work -- pretty cool tech."
    res2 = validate_candidate_message_style(msg2, content_type="connection_note")
    assert not res2["valid"]
    assert "PROHIBITED_PUNCTUATION" in res2["error_codes"]


def test_operational_assumptions_fail_claims():
    """Operational assumptions in questions without prospect evidence fail claims check."""
    from openoutreach.core.content_style import validate_generated_content_claims

    context = {"title": "Software Engineer", "company": "Acme Inc"}

    # 1. Models in prod assumption
    msg_prod = "hey alex, saw your work at acme... what models are you guys running in prod?"
    res_prod = validate_generated_content_claims(msg_prod, context=context)
    assert not res_prod["valid"]
    assert any("production" in e.lower() or "prod" in e.lower() for e in res_prod["errors"])

    # 2. Deploying agents assumption
    msg_agent = "hey alex, saw your work at acme... how are you guys deploying agents rn?"
    res_agent = validate_generated_content_claims(msg_agent, context=context)
    assert not res_agent["valid"]
    assert any("agent" in e.lower() for e in res_agent["errors"])

    # 3. Kubernetes assumption
    msg_k8s = "hey alex, saw your work at acme... how are you handling kubernetes at scale?"
    res_k8s = validate_generated_content_claims(msg_k8s, context=context)
    assert not res_k8s["valid"]
    assert any("kubernetes" in e.lower() for e in res_k8s["errors"])


def test_inbound_and_followup_slang_passes_style():
    """Inbound replies, follow-ups, and natural conversational slang pass Candidate style check."""
    valid_msgs = [
        ("appreciate the note mate... happy to chat next week", "inbound_reply"),
        ("yeah that makes sense... let me check my schedule", "inbound_reply"),
        ("nah all good, appreciate the quick reply", "inbound_reply"),
        ("fair call... we ended up taking a different approach there tbh", "follow_up"),
        ("hey mark, just circling back on this... no rush at all", "follow_up"),
        ("sounds good mate 🙌 let's connect Thursday then", "meeting_transition"),
    ]
    for msg, ctype in valid_msgs:
        res = validate_candidate_message_style(msg, content_type=ctype)
        assert res["valid"], f"Valid chat message failed [{ctype}]: {msg} | Errors: {res.get('errors')}"


def test_terse_fallback_generation():
    """Terse Candidate fallback generator produces valid Candidate style messages."""
    from openoutreach.core.agents.connection_note import generate_terse_candidate_fallback

    # Candidate 1: Company + Title
    p1 = {"first_name": "Sam", "company": "BHP", "title": "Exploration Geologist"}
    fb1 = generate_terse_candidate_fallback(p1)
    res1 = validate_candidate_message_style(fb1, content_type="connection_note")
    assert res1["valid"], f"Fallback 1 failed style: {fb1} | Errors: {res1.get('errors')}"
    assert "sam" in fb1.lower()

    # Candidate 2: Minimal info
    p2 = {"first_name": "Alex"}
    fb2 = generate_terse_candidate_fallback(p2)
    res2 = validate_candidate_message_style(fb2, content_type="connection_note")
    assert res2["valid"], f"Fallback 2 failed style: {fb2} | Errors: {res2.get('errors')}"


def test_zisis_unsupported_prod_assumption_regression():
    """Exact regression: draft assuming models in production fails truth check with UNSUPPORTED_PROSPECT_CLAIM."""
    from openoutreach.core.content_style import (
        validate_candidate_message_style,
        validate_generated_content_claims,
        validate_linkedin_outbound_message,
    )

    zisis_facts = {
        "first_name": "Zisis",
        "headline": "Machine Learning and Engineering Manager at Pinterest",
        "location": "San Francisco Bay Area",
    }

    draft = "hey zisis, saw your ml engineering work at pinterest... curious what models you guys are running in prod rn?"

    # 1. Style validation passes (Candidate casual voice, lowercase, ellipsis, rn)
    style_res = validate_candidate_message_style(draft, content_type="first_message", context=zisis_facts)
    assert style_res["valid"] is True
    assert style_res["error_codes"] == []

    # 2. Truth validation fails (no local facts proving models running in prod)
    claims_res = validate_generated_content_claims(draft, context=zisis_facts)
    assert claims_res["valid"] is False
    assert any("production" in e.lower() or "prod" in e.lower() for e in claims_res["errors"])

    # 3. Canonical outbound gate fails with canonical reason and error code
    outbound_res = validate_linkedin_outbound_message(
        message_text=draft,
        content_type="first_message",
        prospect_facts=zisis_facts,
    )
    assert outbound_res["allowed"] is False
    assert outbound_res["reason"] == "MESSAGE_BLOCKED_CLAIMS"
    assert "UNSUPPORTED_PROSPECT_CLAIM" in outbound_res["error_codes"] or "UNSUPPORTED_CLAIM" in outbound_res["error_codes"]


def test_zisis_safe_open_question_variant():
    """Safe open question variant passes both style and truth validation."""
    from openoutreach.core.content_style import (
        validate_candidate_message_style,
        validate_generated_content_claims,
        validate_linkedin_outbound_message,
    )

    zisis_facts = {
        "first_name": "Zisis",
        "headline": "Machine Learning and Engineering Manager at Pinterest",
        "location": "San Francisco Bay Area",
    }

    draft = "hey zisis, saw the ml + engineering role at pinterest... curious what you're focused on rn?"

    # 1. Style validation passes
    style_res = validate_candidate_message_style(draft, content_type="first_message", context=zisis_facts)
    assert style_res["valid"] is True
    assert style_res["error_codes"] == []
    assert style_res["style_metrics"]["question_count"] == 1
    assert "CORPORATE_LANGUAGE" not in style_res["error_codes"]
    assert "RESUME_DUMP" not in style_res["error_codes"]
    assert "UNSUPPORTED_FAMILIARITY" not in style_res["error_codes"]

    # 2. Truth validation passes
    claims_res = validate_generated_content_claims(draft, context=zisis_facts)
    assert claims_res["valid"] is True
    assert claims_res["errors"] == []

    # 3. Canonical outbound gate passes
    outbound_res = validate_linkedin_outbound_message(
        message_text=draft,
        content_type="first_message",
        prospect_facts=zisis_facts,
    )
    assert outbound_res["allowed"] is True
    assert outbound_res["reason"] == "OK"


def test_unsupported_question_assumptions_matrix():
    """Questions do not get a truth exemption — unsupported presuppositions must fail claims."""
    from openoutreach.core.content_style import validate_generated_content_claims

    zisis_facts = {
        "first_name": "Zisis",
        "headline": "Machine Learning and Engineering Manager at Pinterest",
        "location": "San Francisco Bay Area",
    }

    unsafe_questions = [
        "what models are you running in prod?",
        "how are you guys deploying agents?",
        "what stack are you using?",
        "how are you handling kubernetes?",
        "what projects are you leading?",
        "how are you scaling the pipeline?",
        "how is your recommendation system built?",
        "what LLMs are you guys deploying?",
        "curious about your inference stack",
        "how is your distributed AI infrastructure set up?",
        "saw your production ML models",
        "saw your recommendation systems",
        "how is the ML platform team structured?",
    ]

    for q in unsafe_questions:
        res = validate_generated_content_claims(q, context=zisis_facts)
        assert res["valid"] is False, f"Question should have failed claims grounding: '{q}'"
        assert len(res["errors"]) > 0



