"""Tests for auto-learn ticker pattern promotion."""
import os, sys, json, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from claude_reviewer import ClaudeReviewer


class TestAutoLearnConfig:
    def test_threshold_exists(self):
        assert hasattr(config, 'AUTO_LEARN_THRESHOLD')
        assert isinstance(config.AUTO_LEARN_THRESHOLD, int)
        assert config.AUTO_LEARN_THRESHOLD >= 2

    def test_auto_learn_enabled_flag(self):
        assert hasattr(config, 'AUTO_LEARN_ENABLED')
        assert isinstance(config.AUTO_LEARN_ENABLED, bool)


class TestExtractionSnapshotTracking:
    """Test that agreed extractions are recorded in history.

    We use ClaudeReviewer.__new__() to skip __init__ (which checks CLI
    availability). We then manually set the required instance attributes.
    """

    def test_record_agreement_increments_counter(self, tmp_path):
        history_path = tmp_path / "history.json"
        history_path.write_text("{}")

        reviewer = ClaudeReviewer.__new__(ClaudeReviewer)
        reviewer.available = False
        reviewer._history_path = str(history_path)

        reviewer.record_agreement("CGT", {
            'average_price': 5028.63,
            'shares_transacted': 25981,
            'voting_rights': 15747163,
        })

        history = json.loads(history_path.read_text())
        assert history['CGT']['consecutive_agreements'] == 1

    def test_three_agreements_triggers_promotion(self, tmp_path):
        history_path = tmp_path / "history.json"
        history_path.write_text("{}")

        reviewer = ClaudeReviewer.__new__(ClaudeReviewer)
        reviewer.available = False
        reviewer._history_path = str(history_path)

        snapshot = {
            'average_price': 5028.63,
            'shares_transacted': 25981,
            'voting_rights': 15747163,
        }

        result = None
        for i in range(config.AUTO_LEARN_THRESHOLD):
            result = reviewer.record_agreement("CGT", snapshot)

        assert result == 'promote'

        # Counter should reset after promotion (prevents repeated writes)
        history = json.loads(history_path.read_text())
        assert history['CGT']['consecutive_agreements'] == 0

    def test_disagreement_resets_counter(self, tmp_path):
        history_path = tmp_path / "history.json"
        history_path.write_text(json.dumps({
            "CGT": {"consecutive_agreements": 2, "last_agreed_extraction": {}}
        }))

        reviewer = ClaudeReviewer.__new__(ClaudeReviewer)
        reviewer.available = False
        reviewer._history_path = str(history_path)

        reviewer.record_disagreement("CGT")

        history = json.loads(history_path.read_text())
        assert history['CGT']['consecutive_agreements'] == 0


class TestPatternExtraction:
    """Test that label-value patterns are correctly identified from text."""

    def test_finds_price_label(self):
        text = "Average price paid per share: 5,028.63p"
        from claude_reviewer import extract_label_pattern
        label = extract_label_pattern(text, "5,028.63", field_type='price')
        assert label is not None
        assert "price" in label['start'].lower()

    def test_finds_shares_label(self):
        text = "Number of shares purchased: 25,981"
        from claude_reviewer import extract_label_pattern
        label = extract_label_pattern(text, "25,981", field_type='shares')
        assert label is not None
        assert "shares" in label['start'].lower() or "number" in label['start'].lower()

    def test_returns_none_for_no_label(self):
        text = "The quick brown fox 25,981 jumped over"
        from claude_reviewer import extract_label_pattern
        label = extract_label_pattern(text, "25,981", field_type='shares')
        assert label is None
