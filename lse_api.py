"""Direct API client for LSE announcement text extraction.

Calls the LSE pages API to fetch announcement content as JSON, bypassing
the SPA investor-type gate that can delay content by up to 60 minutes.

The USER-TYPE: PRIVATE_INVESTOR header tells the API to return content
immediately, matching the behaviour of a user who has clicked
"Private Investor" on the disclaimer modal.
"""

import html
import logging
import re
from html.parser import HTMLParser

import requests

import config

logger = logging.getLogger(__name__)

# Tags that should produce a newline boundary when encountered
_BLOCK_TAGS = frozenset([
    "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "hr", "table", "thead", "tbody",
])

# Void elements that only fire handle_starttag and should emit a newline there
_VOID_NEWLINE_TAGS = frozenset(["br"])


# Tags whose text content should be suppressed (CSS, JS)
_SKIP_TAGS = frozenset(["style", "script"])


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML-to-text converter using stdlib only."""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth: int = 0  # > 0 means inside a style/script tag

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()
        if tag_lower in _SKIP_TAGS:
            self._skip_depth += 1
        if tag_lower in _BLOCK_TAGS or tag_lower in _VOID_NEWLINE_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if tag_lower in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag_lower in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def handle_entityref(self, name):
        char = html.unescape(f"&{name};")
        self._parts.append(char)

    def handle_charref(self, name):
        char = html.unescape(f"&#{name};")
        self._parts.append(char)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of 3+ newlines to 2
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        # Collapse multiple spaces on each line (preserve newlines)
        raw = re.sub(r"[^\S\n]+", " ", raw)
        return raw.strip()


def strip_html(html_content: str) -> str:
    """Convert HTML to plain text, preserving block-level line breaks."""
    if not html_content:
        return ""
    parser = _HTMLTextExtractor()
    parser.feed(html_content)
    return parser.get_text()


_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "USER-TYPE": "PRIVATE_INVESTOR",
})

_MIN_BODY_LENGTH = 100  # reject suspiciously short responses


def _extract_body_from_json(data: dict) -> str:
    """Extract announcement body text from LSE pages API JSON response.

    Searches all components for one with type 'news-article-content',
    then extracts and converts the HTML body to plain text.

    Returns empty string if the expected structure is not found.
    """
    components = data.get("components", [])
    for component in components:
        comp_type = component.get("type", "")
        if comp_type != "news-article-content":
            continue
        content_list = component.get("content", [])
        if not content_list:
            continue
        body_html = content_list[0].get("value", {}).get("body", "")
        if body_html:
            return strip_html(body_html)
    return ""


def fetch_announcement_text(article_id: str) -> str:
    """Fetch announcement text directly from the LSE pages API.

    Parameters
    ----------
    article_id:
        The numeric article ID (e.g. '17501130'), extracted from the
        announcement URL path.

    Returns
    -------
    Plain text of the announcement body, or empty string on any failure.
    """
    url = config.API_BASE_URL
    params = {"path": "news-article", "parameters": f"newsId={article_id}"}

    try:
        resp = _session.get(url, params=params, timeout=config.API_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("API request failed for article %s: %s", article_id, exc)
        return ""

    try:
        data = resp.json()
    except ValueError as exc:
        logger.warning("Invalid JSON for article %s: %s", article_id, exc)
        return ""

    text = _extract_body_from_json(data)
    if len(text) < _MIN_BODY_LENGTH:
        logger.warning(
            "API body too short for article %s (%d chars), treating as empty",
            article_id,
            len(text),
        )
        return ""

    logger.debug("API fetched %d chars for article %s", len(text), article_id)
    return text
