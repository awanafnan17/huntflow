# tests/test_ollama_integration.py
"""Comprehensive unit tests for HuntFlow Ollama Local LLM Integration."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
import pytest

from openoutreach.core.models import SiteConfig, is_valid_credential, get_credential_state
from openoutreach.core.llm import (
    split_model_id,
    _validated_site_config,
    build_llm_model,
    get_llm_model,
    prepare_prompt_for_model,
    verify_llm_credentials,
)
from openoutreach.core.onboarding import missing_keys
from openoutreach.core.management.commands.test_llm_config import sanitize_and_classify_llm_error


@pytest.mark.django_db
def test_ollama_provider_requires_no_paid_api_key():
    """Verify Ollama provider requires no paid API key and reports CONFIGURED."""
    cfg = SiteConfig.load()
    cfg.ai_model = "ollama:qwen3:1.7b-q4_K_M"
    cfg.llm_api_key = ""
    cfg.save()

    assert cfg.llm_provider == "ollama"
    assert cfg.is_llm_configured() is True
    assert cfg.get_llm_credential_state() == "CONFIGURED"


@pytest.mark.django_db
def test_remote_provider_requires_configured_credential():
    """Verify remote provider still requires a valid credential."""
    cfg = SiteConfig.load()
    cfg.ai_model = "anthropic:claude-sonnet-4-5-20250929"
    cfg.llm_api_key = ""
    cfg.save()

    assert cfg.llm_provider == "anthropic"
    assert cfg.is_llm_configured() is False
    assert cfg.get_llm_credential_state() == "MISSING"

    cfg.llm_api_key = "test-1-aaaaaaaa"
    cfg.save()
    assert cfg.is_llm_configured() is True
    assert cfg.get_llm_credential_state() == "CONFIGURED"


def test_split_model_id_supports_ollama():
    """Verify split_model_id correctly parses ollama:model."""
    provider, model = split_model_id("ollama:qwen3:1.7b-q4_K_M")
    assert provider == "ollama"
    assert model == "qwen3:1.7b-q4_K_M"


@pytest.mark.django_db
def test_validated_site_config_accepts_ollama_without_key():
    """Verify _validated_site_config accepts ollama provider with blank key."""
    cfg = SiteConfig.load()
    cfg.ai_model = "ollama:qwen3:1.7b-q4_K_M"
    cfg.llm_api_key = ""
    cfg.save()

    loaded = _validated_site_config()
    assert loaded.ai_model == "ollama:qwen3:1.7b-q4_K_M"


@pytest.mark.django_db
def test_onboarding_readiness_for_ollama():
    """Verify missing_keys does not flag llm_api_key when Ollama is configured."""
    cfg = SiteConfig.load()
    cfg.ai_model = "ollama:qwen3:1.7b-q4_K_M"
    cfg.llm_api_key = ""
    cfg.save()

    missing = missing_keys()
    assert "llm_api_key" not in missing


def test_qwen3_thinking_control_for_routine_drafting():
    """Verify prepare_prompt_for_model appends /no_think for Qwen3 routine tasks."""
    prompt = "Draft a 1-sentence connection note for an AI developer."
    prepared = prepare_prompt_for_model(prompt, "ollama:qwen3:1.7b-q4_K_M", is_routine=True)
    assert "/no_think" in prepared

    # Deep analysis does not force /no_think
    deep_prepared = prepare_prompt_for_model(prompt, "ollama:qwen3:1.7b-q4_K_M", is_routine=False)
    assert "/no_think" not in deep_prepared


@pytest.mark.django_db
@patch("openoutreach.core.llm._ping_model")
def test_verify_llm_credentials_ollama_mocked(mock_ping):
    """Verify verify_llm_credentials handles Ollama with fallback defaults."""
    mock_ping.return_value = None
    err = verify_llm_credentials("ollama:qwen3:1.7b-q4_K_M", "", "")
    assert err is None
    mock_ping.assert_called_once_with("ollama:qwen3:1.7b-q4_K_M", "ollama-local", "http://127.0.0.1:11434")


@pytest.mark.django_db
@patch("openoutreach.core.llm._ping_model")
def test_verify_llm_credentials_unreachable_mocked(mock_ping):
    """Verify verify_llm_credentials captures unreachable error."""
    mock_ping.side_effect = ConnectionError("HTTPConnectionPool(host='127.0.0.1', port=11434): Max retries exceeded")
    err = verify_llm_credentials("ollama:qwen3:1.7b-q4_K_M", "", "http://127.0.0.1:11434")
    assert err is not None
    cat, msg = sanitize_and_classify_llm_error(err, "")
    assert cat == "Base URL/network/timeout error"


@pytest.mark.django_db
def test_unrelated_credentials_preserved():
    """Verify configuring Ollama leaves BetterContact and other secrets untouched."""
    cfg = SiteConfig.load()
    cfg.bettercontact_api_key = "test-1-aaaaaaaa"
    cfg.save()

    # Change LLM provider to Ollama
    cfg.ai_model = "ollama:qwen3:1.7b-q4_K_M"
    cfg.save()

    reloaded = SiteConfig.load()
    assert reloaded.bettercontact_api_key == "test-1-aaaaaaaa"
