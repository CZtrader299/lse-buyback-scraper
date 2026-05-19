"""Claude Code CLI integration for edge-case extraction and verification.

Invokes the `claude` CLI as a subprocess to extract transaction data from
announcement text when regex extraction fails or produces low-confidence results.
Falls back gracefully if the CLI is not available.
"""

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime

import config
from extractor import Extractor


# Model to use for extraction (haiku is fast, cheap, and accurate enough for
# structured data extraction; avoids burning Opus quota on simple tasks)
CLAUDE_MODEL = 'haiku'

# JSON schema for structured Claude output
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {"type": "string"},
        "event_type": {"type": "string", "enum": ["Buyback", "Issuance"]},
        "price_pence": {"type": "number"},
        "shares_transacted": {"type": "integer"},
        "announce_date": {"type": "string"},
        "transaction_date": {"type": "string"},
        "shares_in_issue": {"type": "integer"},
        "currency": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]}
    },
    "required": ["ticker", "event_type", "confidence"]
}

# Field mapping: Claude response key -> regex data key
FIELD_MAP = {
    'price_pence': 'average_price',
    'shares_transacted': 'shares_transacted',
    'shares_in_issue': 'voting_rights',
    'announce_date': 'announcement_date',
    'transaction_date': 'effective_date',
    'event_type': 'transaction_type',
}

PRICE_RATIO_TOLERANCE = 0.05

# JSON schema for multi-class extraction (e.g. HAN ordinary + HANA A shares)
MULTI_CLASS_SCHEMA = {
    "type": "object",
    "properties": {
        "share_classes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "share_class": {"type": "string"},
                    "event_type": {"type": "string", "enum": ["Buyback", "Issuance"]},
                    "price_pence": {"type": "number"},
                    "shares_transacted": {"type": "integer"},
                    "announce_date": {"type": "string"},
                    "transaction_date": {"type": "string"},
                    "shares_in_issue": {"type": "integer"},
                    "currency": {"type": "string"},
                },
                "required": ["ticker", "share_class", "event_type"]
            }
        }
    },
    "required": ["share_classes"]
}

# Fields that hold dates — must be normalised before comparison
_DATE_FIELDS = {'announcement_date', 'effective_date'}


class ClaudeReviewer:
    """Invoke Claude CLI to extract or verify transaction data."""

    def __init__(self):
        self._history_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'ticker_review_history.json'
        )
        self.available = self._check_cli_available()
        if not self.available:
            print("  ⚠ Claude CLI not found — Claude review disabled for this run")

    @staticmethod
    def _clean_env():
        """Return a copy of os.environ without CLAUDECODE.

        The CLAUDECODE env var is set when running inside a Claude Code
        session.  The CLI refuses to launch nested sessions, so we must
        strip this variable before spawning ``claude`` as a subprocess.
        """
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        return env

    @staticmethod
    def _check_cli_available():
        """Check if the claude CLI command is available.

        Uses shutil.which() to resolve the claude executable. On Windows
        claude ships as a .cmd shim; shutil.which() honors PATHEXT and
        finds claude.cmd / claude.exe correctly. On POSIX it works the
        same as `which claude`. Returning the absolute path also lets the
        actual subprocess call avoid shell=True quoting issues.
        """
        import shutil
        claude_path = shutil.which("claude")
        if not claude_path:
            return False
        try:
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            result = subprocess.run(
                [claude_path, '--version'],
                capture_output=True, text=True, timeout=10,
                env=env,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def build_prompt(self, page_text, ticker):
        """Build the extraction prompt for Claude.

        Args:
            page_text: Full announcement text
            ticker: Ticker symbol

        Returns:
            str: The prompt to send to Claude
        """
        return f"""Extract transaction data from this LSE announcement for ticker {ticker}.

Return a JSON object with these fields:
- ticker: The ticker symbol (string)
- event_type: "Buyback" or "Issuance" (string)
- price_pence: The average price paid per share in pence (number). If price is in pounds (£/GBP), convert to pence by multiplying by 100.
- shares_transacted: Number of shares bought back or issued in this transaction (integer). This is the daily transaction amount, NOT cumulative/aggregate programme totals.
- announce_date: The date this RNS was published, format YYYY-MM-DD (string)
- transaction_date: The actual date the shares were purchased/issued, format YYYY-MM-DD (string). This may differ from announce_date — look for "purchased on", "date of purchase", "settlement on", "traded on" etc.
- shares_in_issue: Total voting rights or shares in issue EXCLUDING treasury shares, after this transaction (integer)
- currency: The currency unit of the price, e.g. "GBp", "GBP", "USD", "EUR" (string)
- confidence: "high" if all fields clearly extracted, "medium" if some uncertainty, "low" if guessing (string)

Important rules:
- For shares_transacted, extract ONLY the number from TODAY's transaction, not cumulative programme totals
- For shares_in_issue, prefer "excluding treasury" figures over "including treasury"
- If a field cannot be determined, omit it from the JSON (do not guess)
- Price should be in PENCE (not pounds) — convert if needed
- EXCEPTION: For FAIR, the price is in USD (not pence). Return the USD price as-is with currency "USD"
- For ICG: The announcement contains a weekly table of DAILY transactions. SUM all daily shares for shares_transacted, calculate the volume-weighted average price (VWAP) for price_pence, and use the LAST date as transaction_date
- For IGET (and any ticker where this applies): If the announcement contains multiple separate block purchases all on the SAME effective date, SUM the shares_transacted across all blocks and calculate the volume-weighted average price (VWAP) for price_pence. Use the shared date as transaction_date.

ANNOUNCEMENT TEXT:
{page_text}"""

    def build_multi_class_prompt(self, page_text, ticker, class_tickers):
        """Build a prompt for multi-share-class extraction.

        Args:
            page_text: Full announcement text
            ticker: Original ticker (e.g. "HAN")
            class_tickers: List of possible class tickers (e.g. ["HAN", "HANA"])
        """
        class_list = ', '.join(class_tickers)

        # Build a class-description block from config if available
        descriptions = getattr(config, 'MULTI_CLASS_DESCRIPTIONS', {})
        class_desc_lines = [
            f'- "{t}" = {descriptions[t]}' for t in class_tickers if t in descriptions
        ]
        class_desc_block = '\n'.join(class_desc_lines) if class_desc_lines else ''

        return f"""Extract transaction data from this LSE announcement for ticker {ticker}.

This announcement may contain data for MULTIPLE share classes: {class_list}

{class_desc_block}

Return a JSON object with a "share_classes" array. Each element represents one share class found in the announcement:
- ticker: The class ticker ("{class_list}") (string)
- share_class: Description of the share class (e.g. "GBP shares", "USD shares", "Ordinary shares") (string)
- event_type: "Buyback" or "Issuance" (string)
- price_pence: Average price per share in pence (number). Convert £/GBP to pence (×100). For USD-denominated shares, return the USD price as-is.
- shares_transacted: Number of shares in this transaction for THIS class only (integer)
- announce_date: RNS publication date, YYYY-MM-DD (string)
- transaction_date: Actual purchase/issue date, YYYY-MM-DD (string)
- shares_in_issue: Total shares in issue for THIS class excluding treasury (integer)
- currency: "GBp", "GBP", "USD" etc. (string)

Rules:
- If only one share class is mentioned, return an array with one element
- If multiple share classes are mentioned in a table, return one element per class
- For shares_transacted, extract ONLY daily transaction amounts (not cumulative)
- For shares_in_issue, prefer "excluding treasury" figures
- Omit fields you cannot determine (do not guess)

ANNOUNCEMENT TEXT:
{page_text}"""

    def review_multi_class(self, page_text, ticker, class_tickers):
        """Extract multi-class data using Claude CLI.

        Returns:
            list of dicts (one per share class), or None if failed
        """
        if not self.available:
            return None

        prompt = self.build_multi_class_prompt(page_text, ticker, class_tickers)

        for attempt in range(1 + config.CLAUDE_MAX_RETRIES):
            try:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False,
                                                  encoding='utf-8') as f:
                    f.write(prompt)
                    temp_path = f.name

                try:
                    with open(temp_path, 'r', encoding='utf-8') as prompt_file:
                        result = subprocess.run(
                            ['claude', '-p', '-', '--output-format', 'json',
                             '--model', CLAUDE_MODEL,
                             '--tools', '',
                             '--no-session-persistence',
                             '--json-schema', json.dumps(MULTI_CLASS_SCHEMA)],
                            capture_output=True, text=True,
                            timeout=config.CLAUDE_TIMEOUT,
                            stdin=prompt_file,
                            env=self._clean_env(),
                        )
                except subprocess.TimeoutExpired:
                    print(f"    ⚠ Claude CLI timed out for multi-class (attempt {attempt + 1})")
                    continue
                finally:
                    os.unlink(temp_path)

                if result.returncode != 0:
                    print(f"    ⚠ Claude CLI exited with code {result.returncode}")
                    continue

                parsed = self.parse_response(result.stdout)
                if parsed and isinstance(parsed.get('share_classes'), list):
                    classes = parsed['share_classes']
                    print(f"    ✓ Claude identified {len(classes)} share class(es)")
                    return classes
                else:
                    print(f"    ⚠ Could not parse multi-class response (attempt {attempt + 1})")

            except Exception as e:
                print(f"    ⚠ Claude CLI error: {e}")

        return None

    def review(self, page_text, ticker):
        """Send announcement text to Claude CLI for extraction.

        Args:
            page_text: Full announcement text
            ticker: Ticker symbol

        Returns:
            dict with extracted fields, or None if review failed
        """
        if not self.available:
            return None

        prompt = self.build_prompt(page_text, ticker)

        for attempt in range(1 + config.CLAUDE_MAX_RETRIES):
            try:
                # Write prompt to temp file to avoid command-line length limits
                with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False,
                                                  encoding='utf-8') as f:
                    f.write(prompt)
                    temp_path = f.name

                try:
                    # Pipe prompt via stdin to avoid Windows CLI length limits (~8191 chars)
                    # Use --tools "" to skip loading tool definitions (saves ~15-20K tokens/call)
                    # Use --model to avoid burning expensive Opus quota on simple extraction
                    # Use --no-session-persistence to avoid saving throwaway sessions
                    with open(temp_path, 'r', encoding='utf-8') as prompt_file:
                        result = subprocess.run(
                            ['claude', '-p', '-', '--output-format', 'json',
                             '--model', CLAUDE_MODEL,
                             '--tools', '',
                             '--no-session-persistence',
                             '--json-schema', json.dumps(EXTRACTION_SCHEMA)],
                            capture_output=True, text=True,
                            timeout=config.CLAUDE_TIMEOUT,
                            stdin=prompt_file,
                            env=self._clean_env(),
                        )
                except subprocess.TimeoutExpired:
                    print(f"    ⚠ Claude CLI timed out (attempt {attempt + 1})")
                    continue
                finally:
                    os.unlink(temp_path)

                if result.returncode != 0:
                    print(f"    ⚠ Claude CLI exited with code {result.returncode}")
                    continue

                parsed = self.parse_response(result.stdout)
                if parsed:
                    return parsed
                else:
                    print(f"    ⚠ Could not parse Claude response (attempt {attempt + 1})")

            except Exception as e:
                print(f"    ⚠ Claude CLI error: {e}")

        return None

    # JSON schema for ticker identification
    _TICKER_ID_SCHEMA = {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "company_name": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["ticker", "confidence"],
    }

    def build_identify_ticker_prompt(self, page_text):
        """Build a prompt to identify the ticker from announcement text."""
        return f"""Identify the London Stock Exchange ticker symbol (TIDM) for the company in this announcement.

Return a JSON object with:
- ticker: The LSE ticker symbol (e.g. "MNTN", "FSFL", "CGT") — uppercase, no exchange suffix
- company_name: The company name as stated in the announcement
- confidence: "high" if clearly stated, "medium" if inferred, "low" if guessing

Look for:
- TIDM or EPIC fields in the header
- The company name, then determine its LSE ticker
- Any other ticker references in the text

ANNOUNCEMENT TEXT:
{page_text}"""

    def identify_ticker(self, page_text):
        """Use Claude CLI to identify the ticker from announcement text.

        Returns:
            Uppercase ticker string, or None if identification failed.
        """
        if not self.available:
            return None

        prompt = self.build_identify_ticker_prompt(page_text)

        for attempt in range(1 + config.CLAUDE_MAX_RETRIES):
            try:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False,
                                                  encoding='utf-8') as f:
                    f.write(prompt)
                    temp_path = f.name

                try:
                    with open(temp_path, 'r', encoding='utf-8') as prompt_file:
                        result = subprocess.run(
                            ['claude', '-p', '-', '--output-format', 'json',
                             '--model', CLAUDE_MODEL,
                             '--tools', '',
                             '--no-session-persistence',
                             '--json-schema', json.dumps(self._TICKER_ID_SCHEMA)],
                            capture_output=True, text=True,
                            timeout=config.CLAUDE_TIMEOUT,
                            stdin=prompt_file,
                            env=self._clean_env(),
                        )
                except subprocess.TimeoutExpired:
                    print(f"    ⚠ Claude CLI timed out for ticker ID (attempt {attempt + 1})")
                    continue
                finally:
                    os.unlink(temp_path)

                if result.returncode != 0:
                    print(f"    ⚠ Claude CLI exited with code {result.returncode}")
                    continue

                parsed = self.parse_response(result.stdout)
                if parsed and parsed.get('ticker'):
                    ticker = parsed['ticker'].upper().strip()
                    confidence = parsed.get('confidence', 'unknown')
                    print(f"    ✓ Claude identified ticker: {ticker} (confidence: {confidence})")
                    return ticker
                else:
                    print(f"    ⚠ Could not parse ticker ID response (attempt {attempt + 1})")

            except Exception as e:
                print(f"    ⚠ Claude CLI error: {e}")

        return None

    @staticmethod
    def parse_response(response_text):
        """Parse Claude CLI JSON response.

        When invoked with --output-format json and --json-schema, the CLI
        returns an envelope where:
          - ``structured_output`` contains the schema-validated dict (preferred)
          - ``result`` contains Claude's narrative explanation (text, not JSON)

        We check ``structured_output`` first, then fall back to parsing
        ``result`` as JSON in case the CLI format changes.
        """
        if not response_text:
            return None

        try:
            envelope = json.loads(response_text)
        except json.JSONDecodeError:
            return None

        # Preferred: structured_output is already a dict matching the schema
        structured = envelope.get('structured_output')
        if isinstance(structured, dict) and structured:
            return structured

        # Fallback: try to parse the 'result' field as JSON
        content = envelope.get('result', '')
        if isinstance(content, str):
            json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
            if json_match:
                content = json_match.group(1)
            try:
                return json.loads(content)
            except (json.JSONDecodeError, TypeError):
                return None

        return content if isinstance(content, dict) else None

    @staticmethod
    def merge(regex_data, claude_data):
        """Merge Claude results with regex results.

        Args:
            regex_data: dict from regex extraction
            claude_data: dict from Claude extraction

        Returns:
            (merged_data, highlights) where highlights tracks what Claude changed
        """
        merged = dict(regex_data)
        highlights = {
            'claude_filled': [],    # Fields where regex was null, Claude filled
            'disagreement': [],     # Fields where regex and Claude disagree
            'agreed': [],           # Fields where both agree
        }

        if not claude_data:
            return merged, highlights

        for claude_key, regex_key in FIELD_MAP.items():
            claude_val = claude_data.get(claude_key)
            if claude_val is None:
                continue

            regex_val = regex_data.get(regex_key)

            if regex_key == 'average_price':
                claude_val = ClaudeReviewer._normalise_ai_price(
                    claude_val, regex_val, claude_data
                )

            if regex_val is None:
                # Regex missed it, Claude found it
                merged[regex_key] = claude_val
                highlights['claude_filled'].append(regex_key)
            else:
                # For date fields, normalise to output format before comparing
                # so "17 March 2026" and "2026-03-17" are recognised as equal.
                if regex_key in _DATE_FIELDS:
                    norm = Extractor.normalize_date_for_output
                    regex_cmp = norm(str(regex_val))
                    claude_cmp = norm(str(claude_val))
                else:
                    regex_cmp = str(regex_val)
                    claude_cmp = str(claude_val)

                if regex_cmp != claude_cmp:
                    # Skip Claude override for effective_date when regex
                    # matched a high-confidence pattern (e.g. "With effect from",
                    # "will be issued for cash on", date ranges).
                    if regex_key == 'effective_date' and regex_data.get('_effective_date_confident'):
                        highlights['agreed'].append(regex_key)
                        continue
                    if regex_key == 'average_price':
                        if ClaudeReviewer._prices_equivalent(regex_val, claude_val):
                            highlights['agreed'].append(regex_key)
                            continue
                        if not ClaudeReviewer._should_override_price(regex_val, claude_val):
                            highlights['disagreement'].append(regex_key)
                            continue
                    # Genuine disagreement — use Claude's value
                    merged[regex_key] = claude_val
                    highlights['disagreement'].append(regex_key)
                else:
                    highlights['agreed'].append(regex_key)

        return merged, highlights

    @staticmethod
    def _coerce_float(value):
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).replace(',', '').strip())
        except (TypeError, ValueError):
            return None

    @classmethod
    def _normalise_ai_price(cls, ai_value, regex_value=None, ai_data=None):
        """Normalize obvious AI price scale errors before merge."""
        price = cls._coerce_float(ai_value)
        if price is None:
            return ai_value

        regex_price = cls._coerce_float(regex_value)
        if regex_price and price:
            ratio = price / regex_price
            if cls._prices_equivalent(regex_price, price):
                return price
            if abs(ratio - 100) <= 100 * PRICE_RATIO_TOLERANCE:
                return round(price / 100, 6)
            if abs(ratio - 0.01) <= 0.01 * PRICE_RATIO_TOLERANCE:
                return round(price * 100, 6)

        currency = str((ai_data or {}).get('currency', '')).strip()
        currency_lower = currency.lower()
        if currency not in ('GBp', 'GBX') and currency_lower in ('gbp', '£', 'pounds', 'sterling'):
            return round(price * 100, 6)

        return price

    @classmethod
    def _prices_equivalent(cls, left, right):
        left_val = cls._coerce_float(left)
        right_val = cls._coerce_float(right)
        if left_val is None or right_val is None:
            return str(left) == str(right)
        tolerance = max(abs(left_val), abs(right_val), 1) * 0.0001
        return abs(left_val - right_val) <= tolerance

    @classmethod
    def _should_override_price(cls, regex_value, ai_value):
        regex_price = cls._coerce_float(regex_value)
        ai_price = cls._coerce_float(ai_value)
        if regex_price is None:
            return ai_price is not None
        if ai_price is None or ai_price <= 0:
            return False
        ratio = ai_price / regex_price
        return 0.5 <= ratio <= 2.0

    def should_review(self, data, ticker):
        """Determine if this ticker/extraction needs Claude review.

        Args:
            data: dict from regex extraction
            ticker: Ticker symbol

        Returns:
            (bool, str) — whether to review and the reason
        """
        # Always-review list (hardcoded + auto-promoted)
        if ticker in config.ALWAYS_REVIEW_TICKERS:
            return True, 'always_review_list'

        # Check auto-promoted tickers
        history = self._load_history()
        ticker_history = history.get(ticker, {})
        if ticker_history.get('auto_review', False):
            return True, 'auto_promoted'

        # Missing critical fields
        quality = data.get('_data_quality', '')
        if quality != 'ok':
            return True, f'quality_issues: {quality}'

        return False, 'ok'

    def update_history(self, ticker, needed_claude):
        """Update failure tracking for a ticker.

        Args:
            ticker: Ticker symbol
            needed_claude: True if Claude intervention was needed
        """
        history = self._load_history()
        entry = history.get(ticker, {
            'consecutive_failures': 0,
            'auto_review': False,
            'last_failure': None,
        })

        if needed_claude:
            entry['consecutive_failures'] += 1
            entry['last_failure'] = datetime.now().strftime('%Y-%m-%d')
            if entry['consecutive_failures'] >= config.CLAUDE_FAILURE_THRESHOLD:
                entry['auto_review'] = True
                print(f"    ℹ {ticker} auto-promoted to always-review list "
                      f"({entry['consecutive_failures']} consecutive failures)")
        else:
            entry['consecutive_failures'] = 0

        history[ticker] = entry
        self._save_history(history)

    def record_agreement(self, ticker, agreed_fields):
        """Record that regex and Claude agreed on extraction fields.

        Returns 'promote' if threshold reached, 'recorded' otherwise.
        """
        history = self._load_history()
        entry = history.get(ticker, {})

        prev_count = entry.get('consecutive_agreements', 0)
        entry['consecutive_agreements'] = prev_count + 1
        entry['last_agreed_extraction'] = agreed_fields

        history[ticker] = entry
        self._save_history(history)

        if entry['consecutive_agreements'] >= config.AUTO_LEARN_THRESHOLD:
            print(f"    ★ {ticker}: {entry['consecutive_agreements']} consecutive agreements "
                  f"— ready for pattern promotion")
            # Reset counter after promotion to avoid repeated writes on every future run
            entry['consecutive_agreements'] = 0
            history[ticker] = entry
            self._save_history(history)
            return 'promote'
        return 'recorded'

    def record_disagreement(self, ticker):
        """Reset agreement counter when regex and Claude disagree."""
        history = self._load_history()
        entry = history.get(ticker, {})
        entry['consecutive_agreements'] = 0
        entry.pop('last_agreed_extraction', None)
        history[ticker] = entry
        self._save_history(history)

    def _load_history(self):
        """Load ticker review history from JSON file."""
        if os.path.exists(self._history_path):
            try:
                with open(self._history_path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}

    def _save_history(self, history):
        """Save ticker review history to JSON file."""
        with open(self._history_path, 'w') as f:
            json.dump(history, f, indent=2)


def extract_label_pattern(text, value_str, field_type='generic'):
    """Find the label text immediately preceding a known value in announcement text.

    Args:
        text: Full announcement text
        value_str: The string representation of the value to find (e.g. "25,981")
        field_type: One of 'price', 'shares', 'voting_rights', 'generic'

    Returns:
        dict with 'start' and 'end' keys, or None if no clear label found
    """
    # Note: finds first occurrence of value_str. If the same number appears
    # earlier in the text (e.g. in a summary), it may latch onto the wrong
    # instance. Acceptable for initial implementation since most announcements
    # have unique field values in the label-value section.
    idx = text.find(value_str)
    if idx == -1:
        return None

    search_start = max(0, idx - 100)
    before_text = text[search_start:idx].strip()

    lines = [l.strip() for l in before_text.split('\n') if l.strip()]
    if not lines:
        return None

    label = lines[-1].rstrip()

    after_start = idx + len(value_str)
    after_text = text[after_start:after_start + 50].strip()
    end_marker = after_text.split('\n')[0].strip()[:20] if after_text else ''

    label_lower = label.lower()
    relevance_keywords = {
        'price': ('price', 'cost', 'average', 'paid'),
        'shares': ('shares', 'number', 'purchased', 'bought', 'transacted'),
        'voting_rights': ('voting', 'rights', 'issue', 'total', 'capital'),
        'generic': ('price', 'shares', 'number', 'voting', 'total'),
    }
    keywords = relevance_keywords.get(field_type, relevance_keywords['generic'])
    if not any(kw in label_lower for kw in keywords):
        return None

    return {'start': label, 'end': end_marker if end_marker else '\n'}
