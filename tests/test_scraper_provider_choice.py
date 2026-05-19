"""Tests for interactive scraper provider selection."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper


def test_choose_ai_provider_uses_default_on_blank(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert scraper._choose_ai_provider("ollama") == "ollama"


def test_choose_ai_provider_accepts_claude_cli(monkeypatch):
    # In the public menu, option "4" is Claude CLI.
    monkeypatch.setattr("builtins.input", lambda _prompt: "4")
    assert scraper._choose_ai_provider("ollama") == "claude_cli"


def test_choose_ai_provider_invalid_selection_falls_back(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "bad")
    assert scraper._choose_ai_provider("claude_cli") == "claude_cli"


def test_choose_ai_provider_accepts_anthropic_api(monkeypatch):
    # Option "2" in the public menu is Anthropic API.
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")
    assert scraper._choose_ai_provider("auto") == "anthropic_api"


def test_choose_ai_provider_accepts_openai_api(monkeypatch):
    # Option "3" in the public menu is OpenAI API.
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")
    assert scraper._choose_ai_provider("auto") == "openai_api"
