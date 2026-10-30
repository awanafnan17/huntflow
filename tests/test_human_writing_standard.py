import pytest
from unittest.mock import MagicMock, patch
from django.core.exceptions import ValidationError
from openoutreach.core.content_style import validate_user_facing_content_style
from openoutreach.crm.models.deal import Deal, DealState
from openoutreach.core.models import Campaign
from openoutreach.crm.models.lead import Lead
from openoutreach.career.models import ApplicationDraft, JobApplication, CandidateProfile, CandidateProfileVersion, Job
from tests.factories import UserFactory

def test_em_dash_rejection():
    res = validate_user_facing_content_style("Hello — world.", "connection_note")
    assert not res["valid"]
    assert any("em dash" in err for err in res["errors"])

def test_en_dash_rejection():
    res = validate_user_facing_content_style("Hello – world.", "connection_note")
    assert not res["valid"]
    assert any("en dash" in err for err in res["errors"])

def test_double_hyphen_prose_rejection():
    res = validate_user_facing_content_style("Hello -- world.", "connection_note")
    assert not res["valid"]
    assert any("double hyphen" in err for err in res["errors"])

def test_normal_hyphenated_words_allowed():
    res = validate_user_facing_content_style("This is a state-of-the-art self-service tool.", "connection_note")
    assert res["valid"]
    assert len(res["errors"]) == 0

def test_command_flags_allowed():
    res = validate_user_facing_content_style("Run with --dry-run and --verbosity=2.", "connection_note")
    assert res["valid"]
    assert len(res["errors"]) == 0

def test_urls_allowed():
    res = validate_user_facing_content_style("Visit http://example.com/some--path?param=1.", "connection_note")
    assert res["valid"]
    assert len(res["errors"]) == 0

def test_paths_allowed():
    res = validate_user_facing_content_style("File is located at /usr/local/bin/some-script or ./local--file.", "connection_note")
    assert res["valid"]
    assert len(res["errors"]) == 0

def test_inline_code_allowed():
    res = validate_user_facing_content_style("Check `value -- parameter` in the config.", "connection_note")
    assert res["valid"]
    assert len(res["errors"]) == 0

def test_fenced_code_allowed():
    code = """
    Prose here.
    ```python
    # -- or — is allowed in code block
    x = 10
    ```
    More prose.
    """
    res = validate_user_facing_content_style(code, "email")
    assert res["valid"]
    assert len(res["errors"]) == 0

def test_mixed_prose_and_technical_syntax():
    text = "Run `command --flag` or visit http://url.com -- don't hesitate."
    res = validate_user_facing_content_style(text, "email")
    # Should be invalid because of the prose double hyphen '--' outside of inline code / url
    assert not res["valid"]
    assert any("double hyphen" in err for err in res["errors"])

def test_canned_phrase_rejection():
    res = validate_user_facing_content_style("I hope this message finds you well, let's connect.", "connection_note")
    assert not res["valid"]
    assert any("canned phrase" in err for err in res["errors"])

def test_placeholder_rejection():
    res = validate_user_facing_content_style("Hi [First Name], let's connect.", "connection_note")
    assert not res["valid"]
    assert any("Placeholder" in err for err in res["errors"])

def test_connection_note_length():
    res = validate_user_facing_content_style("a" * 301, "connection_note")
    assert not res["valid"]
    assert any("exceeds limit" in err for err in res["errors"])

def test_validation_across_types():
    types = ["job_outreach", "geosentry", "freelance", "scholarship", "cv", "cover_letter", "follow_up", "website", "dashboard", "email"]
    for t in types:
        res = validate_user_facing_content_style("Testing — invalid punctuation", t)
        assert not res["valid"]

@pytest.mark.django_db
def test_persistence_blocking():
    # Style failure blocks saving the Deal model to DB
    user = UserFactory(username="seeker_user_style", email="candidate-10@example.com")
    campaign = Campaign.objects.create(name="Style Test Campaign", is_active=True)
    lead = Lead.objects.create(linkedin_url="https://linkedin.com/in/style-lead", public_identifier="style-lead")
    
    deal = Deal(
        campaign=campaign,
        lead=lead,
        state=DealState.QUALIFIED,
        generated_connection_note="Hello — this is invalid.",
    )
    with pytest.raises(ValidationError, match="Style validation failed"):
        deal.save()

@pytest.mark.django_db
def test_preview_and_sending_blocked_by_style():
    user = UserFactory(username="seeker_user_style2", email="candidate-10@example.com")
    campaign = Campaign.objects.create(name="Style Test Campaign 2", is_active=True)
    lead = Lead.objects.create(linkedin_url="https://linkedin.com/in/style-lead2", public_identifier="style-lead2")
    
    deal = Deal.objects.create(
        campaign=campaign,
        lead=lead,
        state=DealState.QUALIFIED,
        generated_connection_note="Valid connection note",
        generated_follow_up="Valid follow up",
        draft_mode=True
    )
    
    # Try updating with invalid style note
    deal.generated_connection_note = "Invalid — note"
    with pytest.raises(ValidationError, match="Style validation failed"):
        deal.save()

def test_retry_success_and_exhaustion():
    from openoutreach.core.agents.connection_note import generate_connection_note
    
    campaign = MagicMock(spec=Campaign)
    campaign.connection_note_enabled = True
    campaign.product_docs = "Docs"
    campaign.campaign_objective = "Objective"
    campaign.connection_note_instructions = "Instructions"
    campaign.connection_note_template = ""
    campaign.name = "Test Campaign"
    
    profile = {"first_name": "Candidate", "last_name": "Morgan"}
    
    def make_mock_agent_runner(returns_list):
        def _side_effect(coro, *args, **kwargs):
            if hasattr(coro, "close"):
                try:
                    coro.close()
                except Exception:
                    pass
            return returns_list.pop(0)
        return _side_effect

    # 1. Retry Success: first attempt returns invalid, second attempt returns valid
    mock_run = MagicMock()
    mock_run.output.note = "Invalid — text"
    
    mock_run_success = MagicMock()
    mock_run_success.output.note = "Valid clean text"
    
    with patch("openoutreach.core.models.SiteConfig.load") as mock_load, \
         patch("openoutreach.core.llm.run_agent_sync") as mock_run_agent:
        
        cfg = MagicMock()
        cfg.llm_api_key = "fake_key"
        cfg.ai_model = "openai:fake_model"
        mock_load.return_value = cfg
        
        # Returns invalid first, then valid
        mock_run_agent.side_effect = make_mock_agent_runner([mock_run, mock_run_success])
        
        note_text, source = generate_connection_note(None, campaign, profile)
        assert source == "llm"
        assert note_text == "Valid clean text"
        assert mock_run_agent.call_count == 2

    # 2. Retry Exhaustion: all 3 attempts return invalid
    with patch("openoutreach.core.models.SiteConfig.load") as mock_load, \
         patch("openoutreach.core.llm.run_agent_sync") as mock_run_agent:
        
        cfg = MagicMock()
        cfg.llm_api_key = "fake_key"
        cfg.ai_model = "openai:fake_model"
        mock_load.return_value = cfg
        
        # All 3 attempts return invalid style
        mock_run_agent.side_effect = make_mock_agent_runner([mock_run, mock_run, mock_run])
        
        # Falls back to template on retry exhaustion of LLM
        note_text, source = generate_connection_note(None, campaign, profile)
        assert source == "template"
        assert "Candidate" in note_text

@pytest.mark.django_db
def test_historical_evidence_preservation():
    # Verify that existing ApplicationDraft ID 1 (or other historical objects) is not mutated
    user = UserFactory(username="seeker_user_hist", email="candidate-10@example.com")
    profile = CandidateProfile.objects.create(user=user, full_name="idwsdsjtasdkk", email="candidate-10@example.com")
    v3 = CandidateProfileVersion.objects.create(
        profile=profile,
        version_number=3,
        full_name="idwsdsjtasdkk",
        email="candidate-10@example.com",
        created_by=user,
        is_finalized=True,
        state="approved"
    )
    job = Job.objects.create(pk=999, title="Historical Job", company="GenOO")
    job_app = JobApplication.objects.create(pk=999, candidate_profile=profile, job=job)
    draft = ApplicationDraft.objects.create(
        application=job_app,
        version_number=1,
        tailored_cv="CV with em-dash — and idwsdsjtasdkk",
        cover_letter="Cover letter with double-hyphen -- and idwsdsjtasdkk"
    )
    
    # Retrieve from DB and verify they are unchanged/not modified by style validators
    draft_db = ApplicationDraft.objects.get(pk=draft.pk)
    assert draft_db.tailored_cv == "CV with em-dash — and idwsdsjtasdkk"
    assert draft_db.cover_letter == "Cover letter with double-hyphen -- and idwsdsjtasdkk"
