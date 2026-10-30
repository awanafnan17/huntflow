# tests/test_master_candidate_messaging_policy.py
import pytest
from openoutreach.core.content_style import validate_user_facing_content_style, validate_generated_content_claims

def test_prohibited_canned_phrases_and_punctuation():
    bad_messages = [
        "Hi, thanks for connecting! Hope this message finds you well.",
        "Appreciate the connect -- let us supercharge your workflow.",
        "Hey! We build AI automation — would love to book a call?",
        "Hi John, how are you? What tools do you use? Can we chat next week?", # 2 questions
    ]
    for msg in bad_messages:
        res = validate_user_facing_content_style(msg, content_type="connection_note")
        assert not res["valid"], f"Message should have failed policy: {msg}"

def test_valid_candidate_style_messages():
    profiles = [
        {
            "name": "Alex Vance",
            "title": "CTO",
            "company": "DeepScale AI",
            "msg": "saw deepscale is working on custom agent infrastructure. how have you guys been handling real-time browser session persistence?"
        },
        {
            "name": "Sarah Jenkins",
            "title": "SaaS Founder",
            "company": "FlowCRM",
            "msg": "noticed flowcrm's focus on automated lead tracking. curious what part of pipeline sync has been hardest to stabilize"
        },
        {
            "name": "Marcus Sterling",
            "title": "Managing Director",
            "company": "Apex Ventures",
            "msg": "noticed apex's recent investments in developer infrastructure. interested in your take on autonomous agent tooling"
        },
        {
            "name": "Elena Rostova",
            "title": "Head of Product",
            "company": "FintechHub",
            "msg": "saw fintechhub is scaling open banking integrations. how are you managing real-time webhooks at scale?"
        },
        {
            "name": "David Miller",
            "title": "CEO & Founder",
            "company": "CloudForge",
            "msg": "noticed cloudforge's recent launch in European tech. how has candidate matching performed across regional markets?"
        }
    ]

    messages = [p["msg"] for p in profiles]
    assert len(set(messages)) == 5, "All messages must be distinct"

    for p in profiles:
        msg = p["msg"]
        # Style check
        res_style = validate_user_facing_content_style(msg, content_type="connection_note")
        assert res_style["valid"], f"Valid message failed style check: {res_style['errors']}"

        # Claims check
        res_claims = validate_generated_content_claims(msg, context={"title": p["title"]})
        assert res_claims["valid"], f"Valid message failed claim check: {res_claims['errors']}"

        # Length check
        assert len(msg) <= 300, "Message length exceeded 300 characters"

        # Question limit check
        assert msg.count("?") <= 1, "Max 1 question per message"

        # Pitch check
        assert "book a call" not in msg.lower()
        assert "we sell" not in msg.lower()
        assert "our services" not in msg.lower()

        # Prohibited punctuation
        assert "—" not in msg
        assert "--" not in msg
