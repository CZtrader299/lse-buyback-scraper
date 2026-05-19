"""Tests for BHMG/BHMU multi-class extraction and shares multiplier logic.

BHMG = BH Macro Ltd GBP shares  (shares × 1.471 → voting rights equivalent)
BHMU = BH Macro Ltd USD shares  (shares × 0.7606 → voting rights equivalent)

The announcement is published under BHMG's LSE URL and contains a table
listing transactions for both share classes.  Claude extracts each class
separately; the multiplier is applied by _apply_ticker_overrides().
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from extractor import Extractor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_extractor():
    """Return an Extractor without a patterns DB (regex-only mode)."""
    return Extractor(patterns_db_path="__nonexistent__")


# ---------------------------------------------------------------------------
# Config sanity
# ---------------------------------------------------------------------------

class TestConfig:

    def test_bhmg_not_in_manual_only(self):
        """BHMG must no longer be flagged as manual-only."""
        assert "BHMG" not in config.MANUAL_ONLY_TICKERS

    def test_bhmg_in_multi_class_tickers(self):
        assert "BHMG" in config.MULTI_CLASS_TICKERS
        assert config.MULTI_CLASS_TICKERS["BHMG"] == ["BHMG", "BHMU"]

    def test_bhmg_shares_multiplier(self):
        assert config.TICKER_OVERRIDES["BHMG"]["shares_multiplier"] == 1.471

    def test_bhmu_shares_multiplier(self):
        assert config.TICKER_OVERRIDES["BHMU"]["shares_multiplier"] == 0.7606

    def test_multi_class_descriptions_present(self):
        descs = config.MULTI_CLASS_DESCRIPTIONS
        assert descs["BHMG"] == "GBP shares"
        assert descs["BHMU"] == "USD shares"


# ---------------------------------------------------------------------------
# _apply_ticker_overrides — shares_multiplier
# ---------------------------------------------------------------------------

class TestSharesMultiplier:

    def test_bhmg_multiplier_applied(self):
        extractor = _make_extractor()
        data = {'ticker': 'BHMG', 'shares_transacted': 10000, 'average_price': 3500.0}
        extractor._apply_ticker_overrides(data)
        # 10000 × 1.471 = 14710
        assert data['shares_transacted'] == 14710

    def test_bhmu_multiplier_applied(self):
        extractor = _make_extractor()
        data = {'ticker': 'BHMU', 'shares_transacted': 10000, 'average_price': 3200.0}
        extractor._apply_ticker_overrides(data)
        # 10000 × 0.7606 = 7606
        assert data['shares_transacted'] == 7606

    def test_bhmg_multiplier_rounds_to_int(self):
        extractor = _make_extractor()
        # 7 × 1.471 = 10.297 → rounds to 10
        data = {'ticker': 'BHMG', 'shares_transacted': 7, 'average_price': 3500.0}
        extractor._apply_ticker_overrides(data)
        assert data['shares_transacted'] == 10
        assert isinstance(data['shares_transacted'], int)

    def test_bhmu_multiplier_rounds_to_int(self):
        extractor = _make_extractor()
        # 3 × 0.7606 = 2.2818 → rounds to 2
        data = {'ticker': 'BHMU', 'shares_transacted': 3, 'average_price': 3200.0}
        extractor._apply_ticker_overrides(data)
        assert data['shares_transacted'] == 2
        assert isinstance(data['shares_transacted'], int)

    def test_multiplier_not_applied_when_shares_none(self):
        extractor = _make_extractor()
        data = {'ticker': 'BHMG', 'shares_transacted': None, 'average_price': 3500.0}
        extractor._apply_ticker_overrides(data)
        assert data['shares_transacted'] is None

    def test_price_not_affected_by_multiplier(self):
        extractor = _make_extractor()
        data = {'ticker': 'BHMG', 'shares_transacted': 10000, 'average_price': 3500.0}
        extractor._apply_ticker_overrides(data)
        assert data['average_price'] == 3500.0

    def test_bhmg_voting_rights_multiplied(self):
        extractor = _make_extractor()
        data = {'ticker': 'BHMG', 'shares_transacted': 10000, 'voting_rights': 5000000}
        extractor._apply_ticker_overrides(data)
        # 5000000 × 1.471 = 7355000
        assert data['voting_rights'] == 7355000

    def test_bhmu_voting_rights_multiplied(self):
        extractor = _make_extractor()
        data = {'ticker': 'BHMU', 'shares_transacted': 10000, 'voting_rights': 3000000}
        extractor._apply_ticker_overrides(data)
        # 3000000 × 0.7606 = 2281800
        assert data['voting_rights'] == 2281800

    def test_voting_rights_none_not_affected(self):
        extractor = _make_extractor()
        data = {'ticker': 'BHMG', 'shares_transacted': 10000, 'voting_rights': None}
        extractor._apply_ticker_overrides(data)
        assert data['voting_rights'] is None

    def test_unrelated_ticker_unaffected(self):
        extractor = _make_extractor()
        data = {'ticker': 'VOF', 'shares_transacted': 10000, 'average_price': 500.0}
        extractor._apply_ticker_overrides(data)
        assert data['shares_transacted'] == 10000


# ---------------------------------------------------------------------------
# Claude multi-class prompt — BHMG/BHMU descriptions
# ---------------------------------------------------------------------------

class TestMultiClassPrompt:

    def test_bhmg_prompt_contains_class_descriptions(self):
        from claude_reviewer import ClaudeReviewer
        reviewer = ClaudeReviewer.__new__(ClaudeReviewer)
        prompt = reviewer.build_multi_class_prompt("announcement text", "BHMG", ["BHMG", "BHMU"])
        assert "BHMG" in prompt
        assert "BHMU" in prompt
        assert "GBP shares" in prompt
        assert "USD shares" in prompt

    def test_han_prompt_still_contains_correct_descriptions(self):
        from claude_reviewer import ClaudeReviewer
        reviewer = ClaudeReviewer.__new__(ClaudeReviewer)
        prompt = reviewer.build_multi_class_prompt("announcement text", "HAN", ["HAN", "HANA"])
        assert "ordinary shares" in prompt
        assert "A non-voting ordinary shares" in prompt
