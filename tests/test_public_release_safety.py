"""Regression checks for sanitization changes, with no external calls."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openoutreach.career import service


def test_extraction_failure_does_not_invent_candidate_facts(monkeypatch):
    def fail_provider():
        raise ValueError("provider unavailable")

    monkeypatch.setattr(service, "get_llm_model", fail_provider)
    with pytest.raises(RuntimeError, match="Candidate AI generation failed"):
        service.run_factual_extraction("A candidate-supplied source document")


def test_tailoring_failure_does_not_return_a_fixed_resume(monkeypatch):
    monkeypatch.setattr(service, "build_candidate_profile_text", lambda _: "Approved facts")
    monkeypatch.setattr(service, "get_llm_model", lambda: object())
    monkeypatch.setattr(service, "Agent", MagicMock())

    def fail_generation(_):
        raise ValueError("model unavailable")

    monkeypatch.setattr(service, "run_agent_sync", fail_generation)
    with pytest.raises(RuntimeError, match="no candidate facts were fabricated"):
        service.tailor_application(object(), SimpleNamespace(description="Fictional role"))
