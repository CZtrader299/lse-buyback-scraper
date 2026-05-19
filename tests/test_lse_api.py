"""Tests for LSE direct API client."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lse_api import strip_html


class TestStripHtml:
    def test_strips_simple_tags(self):
        assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_preserves_plain_text(self):
        assert strip_html("no tags here") == "no tags here"

    def test_br_becomes_newline(self):
        result = strip_html("line one<br>line two<br/>line three")
        assert "line one\nline two\nline three" in result

    def test_p_and_div_become_newlines(self):
        result = strip_html("<p>para one</p><p>para two</p>")
        assert "para one" in result
        assert "para two" in result
        assert "\n" in result

    def test_decodes_html_entities(self):
        assert strip_html("5,000 &amp; counting &lt;3") == "5,000 & counting <3"

    def test_empty_input(self):
        assert strip_html("") == ""

    def test_collapses_excessive_whitespace(self):
        result = strip_html("<p>  lots   of   space  </p>")
        assert "  lots   of   space  " not in result or "lots of space" in result

    def test_strips_style_tag_content(self):
        result = strip_html("<style>body { color: red; }</style><p>Visible text</p>")
        assert "color" not in result
        assert "Visible text" in result

    def test_strips_script_tag_content(self):
        result = strip_html("<script>var x = 1;</script><p>Visible text</p>")
        assert "var x" not in result
        assert "Visible text" in result


import requests
from unittest.mock import patch, MagicMock
from lse_api import fetch_announcement_text, _extract_body_from_json


class TestExtractBodyFromJson:
    def test_extracts_body_from_valid_structure(self):
        data = {
            "components": [
                {"type": "news-article-issuer-widget"},
                {
                    "type": "news-article-content",
                    "content": [
                        {
                            "value": {
                                "body": "<p>Company purchased <b>10,000</b> shares at 500p.</p>"
                            }
                        }
                    ],
                },
            ]
        }
        result = _extract_body_from_json(data)
        assert "Company purchased 10,000 shares at 500p." in result

    def test_returns_empty_on_missing_components(self):
        assert _extract_body_from_json({}) == ""

    def test_returns_empty_on_wrong_structure(self):
        data = {"components": [{"type": "footer"}]}
        assert _extract_body_from_json(data) == ""

    def test_returns_empty_on_empty_body(self):
        data = {
            "components": [
                {"type": "widget"},
                {"type": "news-article-content", "content": [{"value": {"body": ""}}]},
            ]
        }
        assert _extract_body_from_json(data) == ""

    def test_searches_all_components_for_content(self):
        """API may change component order; function should find it anywhere."""
        data = {
            "components": [
                {"type": "footer"},
                {"type": "footer"},
                {
                    "type": "news-article-content",
                    "content": [{"value": {"body": "<p>Found it</p>"}}],
                },
            ]
        }
        result = _extract_body_from_json(data)
        assert "Found it" in result


class TestFetchAnnouncementText:
    @patch("lse_api._session")
    def test_returns_text_on_successful_api_call(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "components": [
                {"type": "widget"},
                {
                    "type": "news-article-content",
                    "content": [
                        {"value": {"body": "<p>The Company today purchased a total of 5,000 ordinary shares at an average price of 1000p per share, to be held in Treasury.</p>"}}
                    ],
                },
            ]
        }
        mock_session.get.return_value = mock_resp

        result = fetch_announcement_text("12345678")
        assert "5,000 ordinary shares" in result
        mock_session.get.assert_called_once()

    @patch("lse_api._session")
    def test_returns_empty_on_http_error(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = requests.HTTPError("Server error")
        mock_session.get.return_value = mock_resp

        result = fetch_announcement_text("99999999")
        assert result == ""

    @patch("lse_api._session")
    def test_returns_empty_on_network_error(self, mock_session):
        import requests
        mock_session.get.side_effect = requests.ConnectionError("timeout")

        result = fetch_announcement_text("99999999")
        assert result == ""

    @patch("lse_api._session")
    def test_returns_empty_when_body_too_short(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "components": [
                {"type": "widget"},
                {
                    "type": "news-article-content",
                    "content": [{"value": {"body": "<p>Short</p>"}}],
                },
            ]
        }
        mock_session.get.return_value = mock_resp

        result = fetch_announcement_text("12345678")
        assert result == ""
