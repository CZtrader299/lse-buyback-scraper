"""Configuration for LSE Buyback Scraper."""

import os

# LSE News Explorer URL and filters
LSE_BASE_URL = "https://www.londonstockexchange.com/news"
HEADLINE_TYPES = [72, 76]  # 72=Transaction in Own Shares, 76=Issue of Equity
SECTOR_CODES = [302040, 302020, 302030, 351020]  # Investment trust categories

def build_news_url():
    """Build the full LSE News Explorer URL with query parameters.

    The URL must include tab=news-explorer to activate the filtered News Explorer
    view. Parameter names (headlines=, sectors=) match the LSE SPA's expected
    format — these differ from the display labels (headlinetypes, sectorcodes).
    """
    headlines = ",".join(str(h) for h in HEADLINE_TYPES)
    sectors = ",".join(str(s) for s in SECTOR_CODES)
    return (
        f"{LSE_BASE_URL}?tab=news-explorer"
        f"&headlines={headlines}"
        f"&sectors={sectors}"
        f"&headlinetypes=&excludeheadlines="
    )

# Browser settings
HEADLESS = False  # False for interactive monitoring during development
PAGE_LOAD_TIMEOUT = 20  # seconds
REQUEST_DELAY = 4  # seconds between page requests (LSE SPA needs time to render)
MAX_PAGES = 50  # max pagination pages

# Ticker-specific patterns database
PATTERNS_DB_FILENAME = "Output_model_for_New_Scraper_tips_txt_file__1_.xlsm"

# Tickers with known extraction issues - always send to the AI reviewer for verification
ALWAYS_REVIEW_TICKERS = [
    "BPT",   # Announcement doesn't include voting rights / shares in issue
    "FAIR",  # USD-denominated fund — price needs currency handling
    "HAN",   # Two share classes in one announcement (ordinary + A non-voting)
    "HANA",  # Two share classes in one announcement
    "ICG",   # Weekly aggregation — multiple daily rows
    "IGET",  # Occasional same-day multi-block purchases - needs AI aggregation
    "VOF",   # Date range format in announcements
]

# Tickers that need manual processing — skip extraction, flag for human review
# Value is the reason shown in the Description column
MANUAL_ONLY_TICKERS = {}

# Tickers with multiple share classes in a single announcement
MULTI_CLASS_TICKERS = {
    "HAN": ["HAN", "HANA"],
    "BHMG": ["BHMG", "BHMU"],
}

# Human-readable descriptions for each share class ticker, used in AI prompts.
# Helps the AI reviewer identify which class each row in the announcement table belongs to.
MULTI_CLASS_DESCRIPTIONS = {
    "HAN": "ordinary shares",
    "HANA": "A non-voting ordinary shares",
    "BHMG": "GBP shares",
    "BHMU": "USD shares",
}

# Sibling tickers that share the same LSE announcement (published under both URLs).
# Each ticker maps to its share class keyword and its sibling ticker.
# When an announcement is seen under ticker X, the scraper checks the transaction
# narrative (first ~2000 chars) for class-specific language (e.g. "allotted N Income shares").
# If the announcement is for the sibling's class only, the row is skipped — the sibling
# ticker will produce the correct row when its URL is processed.
# Both classes CAN appear in one announcement (e.g. income + growth allotment on same day);
# in that case both rows are kept.
SHARE_CLASS_TICKERS = {
    "CMPI": {"class_keyword": "income", "sibling": "CMPG"},
    "CMPG": {"class_keyword": "growth", "sibling": "CMPI"},
}

# Ticker duplication: when a transaction is found for the key ticker, duplicate
# the same row for each ticker in the value list (e.g. VOF → VCVOF US)
# Include the full Bloomberg ticker with exchange suffix if it differs from LN
DUPLICATE_TICKERS = {"VOF": ["VCVOF US"]}

# Tickers not in user's database — loaded from Excel file for easy editing
# Edit not_tracked_tickers.xlsx to add/remove tickers (column A, one ticker per row)
NOT_TRACKED_FILENAME = "not_tracked_tickers.xlsx"


def load_not_tracked_tickers():
    """Load not-tracked ticker list from Excel file.

    Returns a list of ticker strings. Falls back to an empty list with a
    warning if the file is missing or unreadable.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, NOT_TRACKED_FILENAME)
    if not os.path.exists(path):
        # Normal first-run state — no warning. The file is optional.
        return []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        tickers = []
        for row in ws.iter_rows(min_row=2, max_col=1, values_only=True):
            val = row[0]
            if val and str(val).strip():
                tickers.append(str(val).strip().replace(' LN', ''))
        wb.close()
        return tickers
    except Exception as e:
        # ASCII-only message: config.py is imported before scraper.py reconfigures
        # stdout to UTF-8, so a bare Windows cp1252 console would crash on unicode.
        print(f"  [warn] Could not load {NOT_TRACKED_FILENAME}: {e}")
        return []


NOT_TRACKED_TICKERS = load_not_tracked_tickers()

# Tickers with special extraction logic
TICKER_OVERRIDES = {
    "ARR": {"clear_shares_patterns": True},  # Broken DB markers, force regex
    "BHMG": {"shares_multiplier": 1.471},    # GBP class: multiply by 1.471 to convert shares to voting rights equivalent
    "BHMU": {"shares_multiplier": 0.7606},   # USD class: multiply by 0.7606 to convert shares to voting rights equivalent
    "CTY": {"shares_divisor": 15},           # One vote per 15 shares
    "CVCE": {"price_multiplier": 100},        # Euro shares: convert EUR → euro cents
    "FAIR": {"currency": "USD"},             # USD fund — don't convert to pence
    "FTF": {"admission_date": True},          # Transaction date = admission date from "application has been made" paragraph
    "CGEO": {"weekly_aggregation": True},     # Weekly vertical table — sum daily rows, VWAP, last date
    "ICG": {"weekly_aggregation": True},     # Sum daily rows, VWAP, last date
    "MNTN": {"price_divisor": 100},          # Schiehallion: price quoted in pence (e.g. 195.00c), output in pounds
    "NBPE": {"date_before_price": True},     # Transaction date appears above price in structured format
}

# AI reviewer settings
# Default to "auto" so we pick the best available provider based on env vars:
# Anthropic API > OpenAI API > Ollama > Claude CLI.
# Options: none, auto, anthropic_api, openai_api, claude_cli, ollama.
AI_REVIEW_PROVIDER = os.getenv("LSE_AI_PROVIDER", "auto").strip().lower()
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "300"))

# Anthropic API settings
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# OpenAI API settings
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Legacy Claude CLI settings
CLAUDE_TIMEOUT = 90  # seconds per invocation
CLAUDE_MAX_RETRIES = 2  # retry twice on timeout/parse failure
CLAUDE_FAILURE_THRESHOLD = 3  # consecutive failures before auto-review

# Stale ticker retry
STALE_RETRY_DELAY = 300  # seconds (5 minutes) between retry attempts
STALE_RETRY_MAX_ATTEMPTS = 2  # retry up to 2 times

# Direct API settings (bypasses SPA investor-type gate)
API_BASE_URL = "https://api.londonstockexchange.com/api/v1/pages"
API_TIMEOUT = 15  # seconds per request
API_REQUEST_DELAY = 2  # seconds between API calls (polite rate limiting)

# Auto-learn ticker patterns
AUTO_LEARN_ENABLED = True  # Automatically learn label-value patterns from agreed extractions
AUTO_LEARN_THRESHOLD = 3   # Consecutive regex+Claude agreements before auto-promoting pattern

# Extraction quality thresholds
MAX_DAILY_SHARES = 10_000_000  # sanity cap for single daily buyback
PRICE_TOO_HIGH = 50_000  # pence
PRICE_TOO_LOW = 1  # pence

# Scraping health check
MIN_EXPECTED_ANNOUNCEMENTS = 10  # warn if fewer found

# Output settings
OUTPUT_SHEET_NAME = "output"
OUTPUT_DESCRIPTION = "AutoImported"

# Logging
LOG_TO_FILE = True  # Write all console output to a log file alongside the Excel
