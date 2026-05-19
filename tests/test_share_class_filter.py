"""Tests for share-class filtering (_detect_transacted_share_classes).

Covers the CMPI/CMPG case where LSE publishes the same RNS announcement under
both sibling ticker URLs but the transaction narrative only mentions one class.
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import _detect_transacted_share_classes


# ---------------------------------------------------------------------------
# Fixture: realistic CMPI announcement excerpt (income-only transaction)
# ---------------------------------------------------------------------------
CMPI_NARRATIVE = (
    "The Board of CT Global Managed Portfolio Trust PLC announces that on "
    "8 April 2026 the Company allotted 100,000 Income shares of £0.046131176 each, "
    "from the Company's general business purposes blocklisting facility at a price of "
    "126.00p per Income share. These Income shares will rank pari passu with the "
    "existing Income shares in issue and dealings are expected to commence on "
    "10 April 2026.\n\n"
    "Following the allotment of the above Income shares the following information is "
    "disclosed in accordance with DTR 5.6.1:\n"
    "Total number of Income shares in issue: 61,234,567\n"
    "Total number of Growth shares in issue: 44,112,890\n"
    "Total voting rights: 105,347,457\n"
)

# Dual-class: both income and growth allotted in one announcement
DUAL_NARRATIVE = (
    "The Board announces that on 8 April 2026 the Company allotted 100,000 Income shares "
    "of £0.046 each at 126.00p per Income share and allotted 50,000 Growth shares of "
    "£0.050 each at 198.00p per Growth share.\n\n"
    "Total number of Income shares in issue: 61,234,567\n"
    "Total number of Growth shares in issue: 44,112,890\n"
)

# Growth-only transaction
CMPG_NARRATIVE = (
    "The Board announces that on 8 April 2026 the Company purchased 75,000 Growth shares "
    "at a price of 198.00p per Growth share.\n\n"
    "Total number of Income shares in issue: 61,234,567\n"
    "Total number of Growth shares in issue: 44,112,890\n"
)

# Announcement with no class-specific transaction language (e.g. generic buyback)
GENERIC_NARRATIVE = (
    "The Company announces that on 8 April 2026 it purchased 50,000 ordinary shares "
    "at an average price of 150p per share.\n\n"
    "Total voting rights: 50,000,000\n"
)


class TestDetectTransactedShareClasses:

    def test_income_only_transaction(self):
        result = _detect_transacted_share_classes(CMPI_NARRATIVE)
        assert result == {'income'}

    def test_growth_only_transaction(self):
        result = _detect_transacted_share_classes(CMPG_NARRATIVE)
        assert result == {'growth'}

    def test_dual_class_transaction(self):
        result = _detect_transacted_share_classes(DUAL_NARRATIVE)
        assert result == {'income', 'growth'}

    def test_generic_no_class_returns_empty_set(self):
        """When no class-specific language is found, returns empty set (no skip)."""
        result = _detect_transacted_share_classes(GENERIC_NARRATIVE)
        assert result == set()

    def test_voting_rights_section_does_not_trigger(self):
        """Voting rights section always lists both classes — must not cause false positives."""
        only_balances = (
            "Total number of Income shares in issue: 61,234,567\n"
            "Total number of Growth shares in issue: 44,112,890\n"
            "Total voting rights: 105,347,457\n"
        )
        result = _detect_transacted_share_classes(only_balances)
        assert result == set()

    def test_per_income_share_pattern(self):
        text = "The Company issued shares at a price of 130.00p per Income share."
        result = _detect_transacted_share_classes(text)
        assert 'income' in result

    def test_per_growth_share_pattern(self):
        text = "The Company issued shares at a price of 200.00p per Growth share."
        result = _detect_transacted_share_classes(text)
        assert 'growth' in result

    def test_quantity_and_par_value_pattern(self):
        text = "The Company allotted 200,000 Growth shares of £0.05 each."
        result = _detect_transacted_share_classes(text)
        assert 'growth' in result

    def test_empty_text_returns_empty_set(self):
        assert _detect_transacted_share_classes("") == set()

    def test_none_text_returns_empty_set(self):
        assert _detect_transacted_share_classes(None) == set()

    def test_ignores_class_keywords_beyond_2000_chars(self):
        """Keywords deep in the text (voting rights table) must not match."""
        # Put the action + class keyword past the 2000-char search window
        padding = "x" * 2100
        text = padding + " allotted 100,000 Income shares of £0.05 each."
        result = _detect_transacted_share_classes(text)
        assert result == set()
