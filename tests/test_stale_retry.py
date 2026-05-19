"""Tests for stale ticker retry logic."""
import os, sys, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


class TestStaleRetryConfig:
    def test_retry_delay_exists(self):
        assert hasattr(config, 'STALE_RETRY_DELAY')
        assert isinstance(config.STALE_RETRY_DELAY, (int, float))
        assert config.STALE_RETRY_DELAY > 0

    def test_retry_max_attempts_exists(self):
        assert hasattr(config, 'STALE_RETRY_MAX_ATTEMPTS')
        assert isinstance(config.STALE_RETRY_MAX_ATTEMPTS, int)
        assert config.STALE_RETRY_MAX_ATTEMPTS >= 1


class TestStaleDetection:
    def test_financial_keywords_detected(self):
        from scraper import _ANNOUNCEMENT_KEYWORDS
        text = "The company purchased 10,000 ordinary shares at a price of 500p per share."
        page_lower = text[:3000].lower()
        assert any(kw in page_lower for kw in _ANNOUNCEMENT_KEYWORDS)

    def test_stale_content_detected(self):
        from scraper import _ANNOUNCEMENT_KEYWORDS
        text = "This page is loading... Please wait while we retrieve the document."
        page_lower = text[:3000].lower()
        assert not any(kw in page_lower for kw in _ANNOUNCEMENT_KEYWORDS)
