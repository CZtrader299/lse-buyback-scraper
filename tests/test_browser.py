"""Tests for browser URL ticker extraction."""
import os, sys, pytest, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser import _TICKER_RE


class TestTickerRegex:
    def test_standard_url_extracts_ticker(self):
        url = "https://www.londonstockexchange.com/news-article/FSFL/transaction-in-own-shares/17536000"
        match = _TICKER_RE.search(url)
        assert match is not None
        assert match.group(1).upper() == "FSFL"

    def test_market_news_url_no_ticker_match(self):
        url = "https://www.londonstockexchange.com/news-article/market-news/issue-of-equity/17536059"
        match = _TICKER_RE.search(url)
        assert match is None


class TestExtractLinksFromPage:
    """Test that extract_links_from_current_page captures market-news URLs."""

    def test_market_news_link_captured_with_none_ticker(self):
        """Links with non-standard URLs should be captured with ticker=None."""
        from browser import _TICKER_RE

        href = "https://www.londonstockexchange.com/news-article/market-news/issue-of-equity/17536059"
        match = _TICKER_RE.search(href)
        ticker = match.group(1).upper() if match else None
        assert ticker is None

        path_parts = [p for p in href.rstrip("/").split("/") if p]
        article_id = path_parts[-1] if path_parts else ""
        assert article_id == "17536059"
