"""Tests for provider-selectable AI reviewer wiring."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_reviewer import AIReviewer


class _Response:
    """Stub of an Ollama /api/tags response.

    Defaults to reporting the configured model as installed, since most tests
    care about provider wiring rather than model availability.
    """

    ok = True

    def __init__(self, models=("qwen2.5:3b",)):
        self._models = list(models)

    def json(self):
        return {"models": [{"name": name} for name in self._models]}


def test_none_provider_disables_review():
    reviewer = AIReviewer("none")
    assert reviewer.provider == "none"
    assert reviewer.available is False


def test_unknown_provider_disables_review():
    reviewer = AIReviewer("made_up_provider")
    assert reviewer.provider == "none"
    assert reviewer.available is False


def test_ollama_provider_available_when_tags_endpoint_responds(monkeypatch):
    def fake_get(_url, timeout):
        assert timeout == 3
        return _Response()

    monkeypatch.setattr("ai_reviewer.requests.get", fake_get)
    reviewer = AIReviewer("ollama")
    assert reviewer.provider == "ollama"
    assert reviewer.available is True


def test_parse_plain_json_object():
    parsed = AIReviewer._parse_plain_json('{"ticker": "CGT", "confidence": "high"}')
    assert parsed == {"ticker": "CGT", "confidence": "high"}


def test_parse_plain_json_code_fence():
    parsed = AIReviewer._parse_plain_json(
        '```json\n{"ticker": "BRGE", "confidence": "high"}\n```'
    )
    assert parsed == {"ticker": "BRGE", "confidence": "high"}


def test_ollama_unavailable_when_configured_model_not_installed(monkeypatch):
    """A running server with the model missing must not count as available.

    Otherwise the request later 404s and "auto" never falls through to another
    provider.
    """

    def fake_get(_url, timeout):
        return _Response(models=("some-other-model:latest",))

    monkeypatch.setattr("ai_reviewer.requests.get", fake_get)
    reviewer = AIReviewer("ollama")
    assert reviewer.provider == "none"
    assert reviewer.available is False


def test_ollama_model_matches_when_config_omits_latest_tag(monkeypatch):
    def fake_get(_url, timeout):
        return _Response(models=("qwen2.5:3b:latest",))

    monkeypatch.setattr("ai_reviewer.requests.get", fake_get)
    monkeypatch.setattr("ai_reviewer.config.OLLAMA_MODEL", "qwen2.5:3b")
    reviewer = AIReviewer("ollama")
    assert reviewer.provider == "ollama"
