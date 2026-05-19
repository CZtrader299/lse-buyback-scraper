"""Tests for day-over-day reconciliation logic."""

import os
import pytest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reconciler import Reconciler


class TestReconciliation:
    """Test shares-in-issue math validation."""

    def test_buyback_math_correct(self):
        prior = {'CGT LN': 15773144}  # yesterday
        today = {
            'ticker': 'CGT LN',
            'transaction_type': 'Buyback',
            'shares_transacted': 25981,
            'voting_rights': 15747163,
        }
        result = Reconciler.validate_row(today, prior)
        assert result['valid'] is True

    def test_buyback_math_mismatch(self):
        prior = {'CGT LN': 15773144}
        today = {
            'ticker': 'CGT LN',
            'transaction_type': 'Buyback',
            'shares_transacted': 25981,
            'voting_rights': 15000000,  # wrong
        }
        result = Reconciler.validate_row(today, prior)
        assert result['valid'] is False
        assert result['expected'] == 15747163

    def test_issuance_math_correct(self):
        prior = {'TEST LN': 1000000}
        today = {
            'ticker': 'TEST LN',
            'transaction_type': 'Issuance',
            'shares_transacted': 50000,
            'voting_rights': 1050000,
        }
        result = Reconciler.validate_row(today, prior)
        assert result['valid'] is True

    def test_ticker_not_in_prior(self):
        prior = {}  # empty
        today = {
            'ticker': 'NEW LN',
            'transaction_type': 'Buyback',
            'shares_transacted': 1000,
            'voting_rights': 99000,
        }
        result = Reconciler.validate_row(today, prior)
        assert result['skipped'] is True

    def test_missing_fields_skipped(self):
        prior = {'CGT LN': 15773144}
        today = {
            'ticker': 'CGT LN',
            'transaction_type': 'Buyback',
            'shares_transacted': None,  # missing
            'voting_rights': 15747163,
        }
        result = Reconciler.validate_row(today, prior)
        assert result['skipped'] is True
