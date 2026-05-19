"""Tests for ticker resolution from announcement text."""
import os, sys, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import resolve_ticker_from_text


class TestResolveTickerFromText:
    def test_finds_tidm_field(self):
        text = """RNS Number : 5186Z
The Schiehallion Fund Limited
TIDM: MNTN
LEI: 213800NQOLJA1JCWXQ56"""
        assert resolve_ticker_from_text(text) == "MNTN"

    def test_finds_tidm_with_colon_space(self):
        text = "Some header\nTIDM : ABC\nMore text"
        assert resolve_ticker_from_text(text) == "ABC"

    def test_finds_ticker_field(self):
        text = "Company Name\nTicker: XYZ\nDate: 2026-04-07"
        assert resolve_ticker_from_text(text) == "XYZ"

    def test_finds_epic_field(self):
        text = "EPIC/TIDM: TEST\nTransaction in Own Shares"
        assert resolve_ticker_from_text(text) == "TEST"

    def test_returns_none_when_no_ticker_found(self):
        text = "This is a generic announcement with no ticker identifiers."
        assert resolve_ticker_from_text(text) is None

    def test_ignores_tidm_deep_in_text(self):
        """Only search first 1500 chars — TIDM fields appear in the header."""
        header = "A" * 1600
        text = header + "\nTIDM: LATE\n"
        assert resolve_ticker_from_text(text) is None

    def test_handles_dotted_ticker(self):
        text = "TIDM: SMT.L\nSome other content"
        result = resolve_ticker_from_text(text)
        assert result in ("SMT.L", "SMT")  # either is acceptable

    def test_finds_ticker_in_parentheses_after_limited(self):
        """RNS announcements often use 'Company Name Limited (TICKER)' format."""
        text = """RNS Number : 5186Z
The Schiehallion Fund Limited (MNTN)
Legal Entity Identifier: 213800NQOLJA1JCWXQ56
Issue of Equity"""
        assert resolve_ticker_from_text(text) == "MNTN"

    def test_finds_ticker_in_parentheses_after_plc(self):
        text = "BlackRock Greater Europe Investment Trust plc (BRGE)\nTransaction in Own Shares"
        assert resolve_ticker_from_text(text) == "BRGE"

    def test_finds_ticker_in_parentheses_after_trust(self):
        text = "Schroder Japan Trust (SJG)\nAnnouncement text"
        assert resolve_ticker_from_text(text) == "SJG"

    def test_tidm_takes_priority_over_parentheses(self):
        """TIDM field should be preferred over parentheses format."""
        text = "Some Fund Limited (WRONG)\nTIDM: RIGHT\nMore text"
        assert resolve_ticker_from_text(text) == "RIGHT"
