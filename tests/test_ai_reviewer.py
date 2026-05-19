"""Tests for provider-selectable AI reviewer wiring."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_reviewer import AIReviewer


class _Response:
    ok = True


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
