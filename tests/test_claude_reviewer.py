"""Tests for Claude CLI reviewer integration."""

import os
import json
import pytest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_reviewer import ClaudeReviewer


@pytest.fixture
def reviewer():
    return ClaudeReviewer()


class TestPromptBuilding:
    """Test that prompts are correctly constructed."""

    def test_builds_prompt_with_page_text(self, reviewer):
        prompt = reviewer.build_prompt("Some announcement text", "CGT")
        assert "CGT" in prompt
        assert "Some announcement text" in prompt
        assert "price" in prompt.lower()
        assert "shares" in prompt.lower()

    def test_builds_prompt_with_ticker(self, reviewer):
        prompt = reviewer.build_prompt("text", "BRGE")
        assert "BRGE" in prompt

    def test_iget_in_always_review_tickers(self):
        """IGET must be in ALWAYS_REVIEW_TICKERS so same-day blocks go to Claude."""
        import config
        assert "IGET" in config.ALWAYS_REVIEW_TICKERS

    def test_prompt_contains_same_day_multiblock_rule(self, reviewer):
        """Prompt must instruct Claude to aggregate same-day block purchases."""
        prompt = reviewer.build_prompt("Some announcement text", "IGET")
        assert "same" in prompt.lower() and "date" in prompt.lower()
        assert "sum" in prompt.lower() or "add" in prompt.lower()
        assert "vwap" in prompt.lower() or "volume-weighted" in prompt.lower()


class TestResponseParsing:
    """Test parsing of Claude CLI JSON responses."""

    def test_parses_structured_output(self, reviewer):
        """The CLI returns structured_output when --json-schema is used."""
        response = json.dumps({
            "type": "result",
            "result": "Some narrative explanation...",
            "structured_output": {
                "ticker": "CGT",
                "event_type": "Buyback",
                "price_pence": 5028.63,
                "shares_transacted": 25981,
                "announce_date": "2026-03-16",
                "transaction_date": "2026-03-16",
                "shares_in_issue": 15747163,
                "confidence": "high"
            }
        })
        parsed = reviewer.parse_response(response)
        assert parsed['ticker'] == 'CGT'
        assert parsed['price_pence'] == 5028.63

    def test_parses_result_as_json_fallback(self, reviewer):
        """Fallback: result field contains JSON string (no structured_output)."""
        response = json.dumps({
            "result": json.dumps({
                "ticker": "BRGE",
                "event_type": "Buyback",
                "price_pence": 200.0,
                "confidence": "high"
            })
        })
        parsed = reviewer.parse_response(response)
        assert parsed['ticker'] == 'BRGE'
        assert parsed['price_pence'] == 200.0

    def test_handles_malformed_json(self, reviewer):
        parsed = reviewer.parse_response("not json at all")
        assert parsed is None

    def test_handles_empty_response(self, reviewer):
        parsed = reviewer.parse_response("")
        assert parsed is None


class TestMultiClassPrompt:
    """Test multi-class prompt building."""

    def test_builds_multi_class_prompt_with_classes(self, reviewer):
        prompt = reviewer.build_multi_class_prompt("Some text", "HAN", ["HAN", "HANA"])
        assert "HAN" in prompt
        assert "HANA" in prompt
        assert "share_classes" in prompt
        assert "ordinary shares" in prompt.lower()
        assert "a non-voting" in prompt.lower()

    def test_builds_multi_class_prompt_with_text(self, reviewer):
        prompt = reviewer.build_multi_class_prompt("Announcement content here", "HAN", ["HAN", "HANA"])
        assert "Announcement content here" in prompt


class TestMultiClassParsing:
    """Test parsing of multi-class Claude responses."""

    def test_parses_two_share_classes(self, reviewer):
        response = json.dumps({
            "type": "result",
            "result": "explanation",
            "structured_output": {
                "share_classes": [
                    {"ticker": "HAN", "share_class": "Ordinary shares", "event_type": "Buyback",
                     "price_pence": 2100.0, "shares_transacted": 5000, "shares_in_issue": 50000000},
                    {"ticker": "HANA", "share_class": "A non-voting ordinary shares", "event_type": "Buyback",
                     "price_pence": 1800.0, "shares_transacted": 3000, "shares_in_issue": 30000000},
                ]
            }
        })
        parsed = reviewer.parse_response(response)
        assert 'share_classes' in parsed
        assert len(parsed['share_classes']) == 2
        assert parsed['share_classes'][0]['ticker'] == 'HAN'
        assert parsed['share_classes'][1]['ticker'] == 'HANA'

    def test_parses_single_share_class(self, reviewer):
        response = json.dumps({
            "type": "result",
            "result": "explanation",
            "structured_output": {
                "share_classes": [
                    {"ticker": "HAN", "share_class": "Ordinary shares", "event_type": "Buyback",
                     "price_pence": 2100.0, "shares_transacted": 5000},
                ]
            }
        })
        parsed = reviewer.parse_response(response)
        assert len(parsed['share_classes']) == 1


class TestMergeLogic:
    """Test merging Claude results with regex results."""

    def test_claude_fills_missing_field(self, reviewer):
        regex_data = {'average_price': None, 'shares_transacted': 1000}
        claude_data = {'price_pence': 500.0, 'shares_transacted': 1000}
        merged, highlights = reviewer.merge(regex_data, claude_data)
        assert merged['average_price'] == 500.0
        assert 'average_price' in highlights['claude_filled']

    def test_claude_agrees_with_regex(self, reviewer):
        regex_data = {'average_price': 500.0, 'shares_transacted': 1000}
        claude_data = {'price_pence': 500.0, 'shares_transacted': 1000}
        merged, highlights = reviewer.merge(regex_data, claude_data)
        assert merged['average_price'] == 500.0
        assert len(highlights['claude_filled']) == 0

    def test_claude_disagrees_with_regex(self, reviewer):
        regex_data = {'average_price': 500.0, 'shares_transacted': 1000}
        claude_data = {'price_pence': 600.0, 'shares_transacted': 1000}
        merged, highlights = reviewer.merge(regex_data, claude_data)
        assert merged['average_price'] == 600.0
        assert 'average_price' in highlights['disagreement']

    def test_ai_price_100x_scale_error_is_normalized(self, reviewer):
        """SMT-style 1,426.00p must not become 142600p."""
        regex_data = {'average_price': 1426.0, 'shares_transacted': None}
        claude_data = {'price_pence': 142600, 'shares_transacted': 1300000}
        merged, highlights = reviewer.merge(regex_data, claude_data)
        assert merged['average_price'] == 1426.0
        assert 'average_price' in highlights['agreed']
        assert 'shares_transacted' in highlights['claude_filled']

    def test_gbpence_currency_is_not_treated_as_pounds(self, reviewer):
        regex_data = {'average_price': 1426.0, 'shares_transacted': None}
        claude_data = {
            'price_pence': 1426,
            'shares_transacted': 1300000,
            'currency': 'GBp',
        }
        merged, highlights = reviewer.merge(regex_data, claude_data)
        assert merged['average_price'] == 1426.0
        assert 'average_price' in highlights['agreed']

    def test_implausible_ai_price_override_is_rejected(self, reviewer):
        regex_data = {'average_price': 461.498, 'shares_transacted': 37056}
        claude_data = {'price_pence': 4614980, 'shares_transacted': 37056}
        merged, highlights = reviewer.merge(regex_data, claude_data)
        assert merged['average_price'] == 461.498
        assert 'average_price' in highlights['disagreement']

    def test_date_formats_match_after_normalization(self, reviewer):
        """Regex '17 March 2026' and Claude '2026-03-17' are the same date."""
        regex_data = {
            'announcement_date': '18 March 2026',
            'effective_date': '17 March 2026',
        }
        claude_data = {
            'announce_date': '2026-03-18',
            'transaction_date': '2026-03-17',
        }
        merged, highlights = reviewer.merge(regex_data, claude_data)
        assert len(highlights['disagreement']) == 0
        assert 'announcement_date' in highlights['agreed']
        assert 'effective_date' in highlights['agreed']


class TestIdentifyTickerPrompt:
    def test_builds_identify_ticker_prompt(self, reviewer):
        prompt = reviewer.build_identify_ticker_prompt("The Schiehallion Fund Limited\nTransaction in Own Shares")
        assert "ticker" in prompt.lower()
        assert "Schiehallion" in prompt
        assert "TIDM" in prompt or "ticker symbol" in prompt.lower()
