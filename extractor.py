"""Regex and ticker-pattern extraction engine.

Extracts transaction data from LSE announcement text using:
1. Ticker-specific patterns from Excel database (highest accuracy)
2. Generic regex patterns (fallback)

This module has no Selenium dependency — it operates on plain text.
"""

import re
import os
from datetime import datetime

import config
from ticker_patterns import TickerPatternsDB


class Extractor:
    """Hybrid extraction engine: ticker patterns + generic regex."""

    def __init__(self, patterns_db_path=None):
        """Initialize extractor, optionally loading ticker-specific patterns.

        Args:
            patterns_db_path: Path to the ticker patterns Excel file.
                            If None, attempts to find it in the project directory.
        """
        self.patterns_db = None

        # Try to load ticker patterns database
        if patterns_db_path is None:
            # Look in project directory
            script_dir = os.path.dirname(os.path.abspath(__file__))
            patterns_db_path = os.path.join(script_dir, config.PATTERNS_DB_FILENAME)

        if patterns_db_path and os.path.exists(patterns_db_path):
            self.patterns_db = TickerPatternsDB(patterns_db_path)
            if self.patterns_db.loaded:
                # Apply ticker overrides from config
                for ticker, overrides in config.TICKER_OVERRIDES.items():
                    if overrides.get('clear_shares_patterns') and self.patterns_db.has_patterns(ticker):
                        patterns = self.patterns_db.get_patterns(ticker)
                        patterns['shares_start'] = ''
                        patterns['shares_end'] = ''
            else:
                self.patterns_db = None
                print("  ⚠ Ticker patterns failed to load — using regex only")
        else:
            print("  ℹ No ticker patterns database found — using regex only")

    def extract(self, page_text, ticker):
        """Extract all transaction fields from announcement text.

        Args:
            page_text: Full announcement text (from Selenium .text)
            ticker: Ticker symbol (without LN suffix)

        Returns:
            dict with keys: ticker, transaction_type, shares_transacted, average_price,
                          currency, announcement_date, effective_date, shares_outstanding,
                          voting_rights, held_in_treasury, cancellation_status,
                          extraction_method, _data_quality
        """
        data = {
            'ticker': ticker,
            'transaction_type': None,
            'shares_transacted': None,
            'average_price': None,
            'currency': 'GBp',
            'announcement_date': None,
            'effective_date': None,
            'shares_outstanding': None,
            'voting_rights': None,
            'held_in_treasury': None,
            'cancellation_status': None,
            'extraction_method': None,
        }

        if not page_text:
            data['_data_quality'] = 'no_text'
            return data

        # HYBRID EXTRACTION: Try ticker-specific patterns first
        if self.patterns_db and self.patterns_db.has_patterns(ticker):
            print(f"  → Trying ticker-specific patterns...")
            pattern_data = self.patterns_db.extract_with_patterns(ticker, page_text)

            if pattern_data and (pattern_data.get('shares_transacted') or pattern_data.get('average_price')):
                print(f"  ✓ Used ticker-specific patterns")

                # Apply currency conversion if price is in £/GBP
                currency = pattern_data.get('currency', 'GBp')
                if pattern_data.get('average_price') and currency and currency.lower() in ('£', 'gbp', 'pounds', 'sterling'):
                    pattern_data['average_price'] = round(pattern_data['average_price'] * 100, 6)
                    pattern_data['_pounds_converted'] = True

                data.update(pattern_data)
                data['extraction_method'] = 'ticker_patterns'

                # Extract common fields (date, type) with regex
                self._extract_common_fields(data, page_text)

                # Supplement missing fields with regex
                missing = [k for k in ('shares_transacted', 'average_price', 'voting_rights', 'shares_outstanding')
                           if data.get(k) is None]
                if missing:
                    print(f"    → Supplementing missing fields: {missing}")
                    supplement = {k: None for k in ('shares_transacted', 'average_price',
                                                     'voting_rights', 'shares_outstanding', 'held_in_treasury')}
                    self._extract_with_regex(supplement, page_text)
                    for field in missing:
                        if supplement.get(field) is not None:
                            data[field] = supplement[field]
            else:
                print(f"  ! Ticker patterns failed, using regex")
                self._extract_with_regex(data, page_text)
                self._extract_common_fields(data, page_text)
                data['extraction_method'] = 'regex_fallback'
        else:
            if self.patterns_db:
                print(f"  → No patterns for {ticker}, using regex")
            self._extract_with_regex(data, page_text)
            data['extraction_method'] = 'regex_only'

        # Apply ticker-specific post-processing
        self._apply_ticker_overrides(data)

        # Sanity checks (pass page_text for multi-transaction detection)
        self._sanity_check(data, page_text)

        # Weekly aggregation for tickers with multi-day tables
        overrides = config.TICKER_OVERRIDES.get(ticker, {})
        if overrides.get('weekly_aggregation') and page_text:
            agg = self._extract_weekly_aggregate(page_text)
            if agg:
                data['shares_transacted'] = agg['total_shares']
                data['average_price'] = agg['vwap']
                data['effective_date'] = agg['last_date']
                print(f"    ℹ Weekly aggregation: {agg['row_count']} days, "
                      f"{agg['total_shares']} shares, VWAP={agg['vwap']}")

        # Assess data quality
        data['_data_quality'] = self.assess_quality(data)

        return data

    def _extract_common_fields(self, data, page_text):
        """Extract date, transaction type, effective date, cancellation status."""
        text_lower = page_text.lower()

        # Transaction type (only set if not already determined by ticker-pattern or regex extraction)
        if data.get('transaction_type') is None:
            if any(word in text_lower for word in ['buyback', 'purchase', 'repurchase', 'buy-back', 'bought']):
                data['transaction_type'] = 'Buyback'
            elif any(word in text_lower for word in ['issue', 'allotment', 'issuance', 'allotted']):
                data['transaction_type'] = 'Issuance'

        # Announcement date (first date in text)
        date_patterns = [
            r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
            r'(\d{1,2}[/-]\d{1,2}[/-]\d{4})',
        ]
        for pattern in date_patterns:
            match = re.search(pattern, page_text)
            if match:
                data['announcement_date'] = self._normalize_date(match.group(1))
                break

        # Effective date
        data['effective_date'], data['_effective_date_confident'] = self._extract_effective_date(page_text, data.get('announcement_date'), ticker=data.get('ticker'))

        # Cancellation
        if 'cancelled' in text_lower:
            data['cancellation_status'] = 'Cancelled'
        elif data.get('held_in_treasury'):
            data['cancellation_status'] = 'Treasury'

    def _extract_effective_date(self, page_text, announcement_date, ticker=None):
        """Extract actual transaction/settlement date from announcement text.

        Falls back to announcement_date if no transaction-specific date found.
        Returns (date_str, confident) tuple where confident=True when an
        explicit date pattern matched (vs. falling back to announcement_date).
        """
        if not page_text:
            return self._normalize_date(announcement_date), False

        # Date sub-patterns (3 formats)
        date_dd_month_yyyy = r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})'
        date_dd_mm_yyyy = r'(\d{1,2}[/\-]\d{1,2}[/\-]\d{4})'
        date_yyyy_mm_dd = r'(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})'
        date_pattern = f'(?:{date_dd_month_yyyy}|{date_dd_mm_yyyy}|{date_yyyy_mm_dd})'

        # Date RANGE patterns (try first — e.g. "period from X to Y", "between X and Y")
        # These use two date_pattern captures (6 groups total); we want the END date (groups 4-6)
        range_patterns = [
            rf'(?:period|week)\s+(?:from\s+)?{date_pattern}\s+to\s+{date_pattern}',
            rf'between\s+{date_pattern}\s+and\s+{date_pattern}',
            rf'from\s+{date_pattern}\s+to\s+{date_pattern}',
            rf'during\s+{date_pattern}\s+(?:to|and|through)\s+{date_pattern}',
        ]
        for pattern in range_patterns:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                # End date is in groups 4-6 (second date_pattern capture)
                end_date = match.group(4) or match.group(5) or match.group(6)
                if end_date:
                    effective = self._normalize_date(end_date)
                    normalized_announced = self._normalize_date(announcement_date)
                    if effective and effective != normalized_announced:
                        print(f"    ℹ Effective date '{effective}' from date range (differs from announcement '{normalized_announced}')")
                    return (effective if effective else normalized_announced), True

        # Date-before-price pattern: Used by NBPE and similar structured formats.
        # Primary: "Date of purchase of Shares\n\n26 March 2026" (explicit label)
        # Fallback: a date on its own line within a few lines of price-related text
        overrides = config.TICKER_OVERRIDES.get(ticker or '', {})
        if overrides.get('date_before_price'):
            # Try explicit label first
            date_label_pattern = rf'[Dd]ate\s+of\s+purchase\s+of\s+[Ss]hares\s*\n\s*{date_pattern}'
            match = re.search(date_label_pattern, page_text, re.IGNORECASE | re.MULTILINE)
            if not match:
                # Fallback: date on its own line near price text (within 5 lines)
                date_before_price_pattern = rf'(?:^|\n)\s*{date_pattern}\s*\n(?:.*\n){{0,5}}.*?(?:price|pence|£|€|\$)'
                match = re.search(date_before_price_pattern, page_text, re.IGNORECASE | re.MULTILINE)
            if match:
                effective = match.group(1) or match.group(2) or match.group(3)
                effective = self._normalize_date(effective) if effective else None
                if effective:
                    normalized_announced = self._normalize_date(announcement_date)
                    if effective != normalized_announced:
                        print(f"    ℹ Effective date '{effective}' from date-before-price pattern (differs from announcement '{normalized_announced}')")
                    return effective, True

        # Admission date pattern: Used by FTF and similar issuance announcements.
        # Extracts the date from "Application has been made for the admission...
        # on or around 31 March 2026" paragraph, which is the actual trading date.
        if overrides.get('admission_date'):
            admission_pattern = rf'application\s+has\s+been\s+made\s+.*?(?:on\s+or\s+around|on|for)\s+{date_pattern}'
            match = re.search(admission_pattern, page_text, re.IGNORECASE | re.DOTALL)
            if match:
                effective = match.group(1) or match.group(2) or match.group(3)
                effective = self._normalize_date(effective) if effective else None
                if effective:
                    normalized_announced = self._normalize_date(announcement_date)
                    if effective != normalized_announced:
                        print(f"    ℹ Effective date '{effective}' from admission pattern (differs from announcement '{normalized_announced}')")
                    return effective, True

        effective_date_patterns = [
            # Future-dated issuance patterns (highest priority)
            rf'(?:shares?\s+)?will\s+be\s+issued\s+(?:for\s+cash\s+)?on\s+{date_pattern}',
            rf'will\s+be\s+admitted\s+(?:to\s+trading\s+)?on\s+{date_pattern}',
            rf'will\s+be\s+allotted\s+on\s+{date_pattern}',
            rf'admission\s+is\s+expected\s+(?:to\s+(?:take\s+place|become\s+effective)\s+)?on\s+{date_pattern}',
            rf'admitted\s+to\s+trading\s+on\s+{date_pattern}',
            rf'dealings\s+(?:are\s+expected\s+to\s+|will\s+)?commence\s+on\s+{date_pattern}',
            rf'application\s+has\s+been\s+made\s+.*?(?:on\s+or\s+around|on|for)\s+{date_pattern}',
            # Label-value patterns (EU MAR format with optional "the")
            rf'date\s+of\s+(?:the\s+)?purchases?\s*[:\-]?\s*{date_pattern}',
            rf'date\s+of\s+(?:the\s+)?transactions?\s*[:\-]?\s*{date_pattern}',
            rf'date\s+of\s+(?:the\s+)?trades?\s*[:\-]?\s*{date_pattern}',
            rf'transaction\s+date\s*[:\-]?\s*{date_pattern}',
            rf'trade\s+date\s*[:\-]?\s*{date_pattern}',
            rf'purchase\s+date\s*[:\-]?\s*{date_pattern}',
            # "With effect from" — UK regulatory language (e.g. BRAI)
            rf'[Ww]ith\s+effect\s+from\s+{date_pattern}',
            # Settlement / admission date patterns
            rf'(?:following\s+)?settlement\s+(?:of\s+this\s+purchase\s+)?on\s+{date_pattern}',
            # Narrative patterns — date before verb ("On 25 March 2026, ...bought back")
            rf'(?:^|\.\s+|\n)\s*on\s+{date_pattern}\s*,\s*.{{0,120}}?(?:bought\s+back|purchased|repurchased)',
            rf'announces\s+that\s+on\s+{date_pattern}',
            rf'purchased\s+on\s+{date_pattern}',
            rf'repurchased\s+on\s+{date_pattern}',
            rf'bought\s+back\s+on\s+{date_pattern}',
            rf'shares?\s+were\s+purchased\s+on\s+{date_pattern}',
            rf'shares?\s+were\s+repurchased\s+on\s+{date_pattern}',
            rf'shares?\s+were\s+bought\s+back\s+on\s+{date_pattern}',
            rf'transactions?\s+(?:took|taken)\s+place\s+on\s+{date_pattern}',
            rf'transactions?\s+occurred\s+on\s+{date_pattern}',
            rf'(?:were\s+)?carried\s+out\s+on\s+{date_pattern}',
            rf'(?:were\s+)?executed\s+on\s+{date_pattern}',
            rf'(?:were\s+)?made\s+on\s+{date_pattern}',
            rf'issued\s+on\s+{date_pattern}',
            rf'allotted\s+on\s+{date_pattern}',
            # Trailing date: "repurchased...on {date}." / "purchased...on {date}." (FAIR-style)
            rf'(?:repurchased|purchased|bought\s+back).{{0,200}}?\bon\s+{date_pattern}\s*[.\n]',
        ]

        for pattern in effective_date_patterns:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                effective = match.group(1) or match.group(2) or match.group(3)
                effective = self._normalize_date(effective) if effective else None
                normalized_announced = self._normalize_date(announcement_date)
                if effective and effective != normalized_announced:
                    print(f"    ℹ Effective date '{effective}' differs from announcement date '{normalized_announced}'")
                return (effective if effective else normalized_announced), True

        ticker_label = ticker or 'unknown'
        # Diagnostic: check if text contains dates other than announcement_date
        all_dates = re.findall(date_dd_month_yyyy, page_text, re.IGNORECASE)
        normalized_announced = self._normalize_date(announcement_date)
        other_dates = [d for d in all_dates if self._normalize_date(d) != normalized_announced]
        if other_dates:
            print(f"    ⚠ No effective date pattern matched for {ticker_label}, "
                  f"but found other dates: {other_dates[:3]} — review manually")
        else:
            print(f"    ⚠ No effective date pattern matched for {ticker_label}")
        return self._normalize_date(announcement_date), False

    def _extract_with_regex(self, data, page_text):
        """Extract all fields using generic regex patterns.

        Modifies data dict in-place. Ported from lse_scraper_fixed.py.
        """
        text_lower = page_text.lower()

        # Transaction type
        if data.get('transaction_type') is None:
            if any(word in text_lower for word in ['buyback', 'purchase', 'repurchase', 'buy-back', 'bought']):
                data['transaction_type'] = 'Buyback'
            elif any(word in text_lower for word in ['issue', 'allotment', 'issuance', 'allotted']):
                data['transaction_type'] = 'Issuance'

        # Announcement date
        if data.get('announcement_date') is None:
            date_patterns = [
                r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
                r'(\d{1,2}[/-]\d{1,2}[/-]\d{4})',
            ]
            for pattern in date_patterns:
                match = re.search(pattern, page_text)
                if match:
                    data['announcement_date'] = self._normalize_date(match.group(1))
                    break

        # Shares transacted — ordered from most specific to least
        # Ported from lse_scraper_fixed.py (28 patterns) with negative lookahead guard
        if data.get('shares_transacted') is None:
            share_patterns = [
                # Aggregate totals first (PIN, ICG, PLUS)
                r'aggregate\s+(?:number\s+of\s+)?(?:ordinary\s+)?shares?\b.*?purchased[\s:]+([\d,]+)',
                # Label-value formats (GCP, AJB, UTG, HGT)
                r'(?:number of|no\.?\s*of).*?shares\s+purchased[:\s]+([\d,]+)',
                r'shares\s+purchased[:\s]+([\d,]+)',
                # Narrative with "in the market" (ANII)
                r'purchased\s+in\s+the\s+market\s+([\d,]+)',
                # Sale from treasury (PCGH)
                r'sale\s+of\s+([\d,]+)\s+(?:ordinary|shares)',
                # Standard narrative patterns
                r'purchase(?:d)?\s+on\s+\d{1,2}\s+\w+\s+\d{4}\s+([\d,]+)\s+(?:ordinary\s+)?shares',
                r'purchase(?:d)?\s+(?:of\s+)?([\d,]+)\s+(?:of\s+its\s+own\s+)?ordinary',
                r'purchase(?:d)?\s+(?:of\s+)?([\d,]+)\s+(?:of\s+its\s+own\s+)?(?:ordinary\s+)?shares',
                # "purchased on behalf of the Company N of its ordinary shares" (MGCI-style intervening text)
                r'purchased\s+.{0,60}?([\d,]+)\s+of\s+its\s+(?:own\s+)?ordinary\s+shares',
                r'repurchased\s+([\d,]+)\s+ordinary',
                r'repurchased\s+([\d,]+)',
                r'bought\s+back\s+([\d,]+)',
                r'purchased\s+a\s+total\s+of\s+([\d,]+)',
                # Reversed order: number before "purchased" (HGT-style)
                r'([\d,]+)\s+ordinary\s+shares.*?purchased',
                # Issuance patterns
                r'issue(?:d)?\s+(?:of\s+)?([\d,]+)\s+(?:of\s+its\s+)?ordinary',
                r'issue(?:d)?\s+([\d,]+)\s+(?:of\s+its\s+)?(?:ordinary\s+)?shares',
                r'allot(?:ted|ment)?\s+(?:of\s+)?([\d,]+)',
                r'total(?:ling)?\s+([\d,]+)\s+shares',
                # Greedy fallback — guarded with negative lookahead to exclude non-transaction counts
                r'([\d,]+)\s+(?:new\s+)?ordinary\s+shares(?!\s+(?:in\s+issue|in\s+aggregate|held|in\s+treasury|with\s+voting))',
            ]
            for pattern in share_patterns:
                match = re.search(pattern, text_lower)
                if match:
                    shares_str = match.group(1).replace(',', '')
                    try:
                        shares = int(shares_str)
                        # Context check: reject matches near non-transaction share counts
                        start = max(0, match.start() - 30)
                        end = min(len(text_lower), match.end() + 30)
                        context = text_lower[start:end]
                        reject_phrases = ('in aggregate', 'held in treasury', 'in treasury',
                                          'in issue', 'with voting')
                        if any(phrase in context for phrase in reject_phrases):
                            if 'aggregate number' not in context:
                                continue
                        data['shares_transacted'] = shares
                        break
                    except ValueError:
                        continue

        # Average price — patterns with currency detection
        if data.get('average_price') is None:
            price_patterns = [
                # (pattern, is_pounds) — is_pounds means multiply by 100
                (r'(?:volume[\s-]+)?weighted\s+average\s+price\s+(?:paid\s+)?(?:per\s+(?:ordinary\s+)?share\s*)?(?:\((?:gbp?|p|pence)\))?[:\s]+([\d,.]+)', False),
                (r'average\s+price\s+(?:paid\s+)?(?:per\s+(?:ordinary\s+)?share\s*)?(?:\((?:gbp?|p|pence)\))?[:\s]+([\d,.]+)\s*(?:p(?:ence)?)?\b', False),
                (r'(?<!highest\s)(?<!lowest\s)price\s+(?:paid\s+)?(?:per\s+(?:ordinary\s+)?share\s*)?(?:\((?:gbp?|p|pence)\))?[:\s]+([\d,.]+)\s*(?:p(?:ence)?)\b', False),
                (r'(?:average\s+)?price\s+of\s+([\d,.]+)\s*(?:p(?:ence)?)\b', False),
                (r'(?:average\s+)?price\s+of\s+£([\d,.]+)', True),
                (r'(?:average\s+)?price\s+of\s+([\d,.]+)\s*(?:gbp|pounds?|sterling)', True),
                (r'at\s+(?:a\s+)?(?:price|an\s+average\s+price)\s+of\s+([\d,.]+)\s*(?:p(?:ence)?)\b', False),
                (r'at\s+(?:a\s+)?(?:price|an\s+average\s+price)\s+of\s+£([\d,.]+)', True),
                (r'at\s+([\d,.]+)\s*(?:p(?:ence)?)\s+(?:per|each)', False),
                (r'at\s+£([\d,.]+)\s+(?:per|each)', True),
                (r'([\d,.]+)\s*(?:p(?:ence)?)\s+per\s+(?:ordinary\s+)?share', False),
                (r'£([\d,.]+)\s+per\s+(?:ordinary\s+)?share', True),
            ]
            for pattern, is_pounds in price_patterns:
                match = re.search(pattern, text_lower)
                if match:
                    price_str = match.group(1).replace(',', '')
                    try:
                        price = float(price_str)
                        if is_pounds:
                            price = round(price * 100, 6)
                            data['_pounds_converted'] = True
                        data['average_price'] = price
                        break
                    except ValueError:
                        continue

        # Voting rights
        if data.get('voting_rights') is None:
            voting_patterns = [
                # Flexible: allow arbitrary text between "company" and "is/are" (handles
                # "excluding treasury shares as at 24 March 2026", "rounded up to the whole number", etc.)
                r'(?:total\s+)?(?:number\s+of\s+)?voting\s+rights?\s+in\s+(?:the\s+)?company\b.{0,80}?\b(?:is|are|will\s+be)\s+([\d,]+)',
                r'total\s+(?:number\s+of\s+)?voting\s+rights?\s*(?::|is|are)\s*([\d,]+)',
                # "Total Voting Rights of the Company attaching to...\n100,290,000" — label then number on next line (PCGH)
                r'total\s+voting\s+rights\s+[\s\S]{0,80}?([\d,]+)',
                # "Total Voting Rights\n403,106,671" — table format with no delimiter
                r'total\s+voting\s+rights\s+([\d,]+)',
                r'(?:figure\s+of\s+)?([\d,]+)\s+(?:ordinary\s+)?shares?\s+represents?\s+the\s+total\s+voting\s+rights',
                r'([\d,]+)\s+(?:total\s+)?voting\s+rights',
                r'a\s+total\s+of\s+([\d,]+)\s+have\s+voting\s+rights',
                r'voting\s+rights\s+in\s+the\s+company\b.{0,80}?\b(?:is|are)\s+([\d,]+)',
            ]
            for pattern in voting_patterns:
                match = re.search(pattern, text_lower)
                if match:
                    vr_str = match.group(1).replace(',', '')
                    try:
                        data['voting_rights'] = int(vr_str)
                        break
                    except ValueError:
                        continue

        # Shares outstanding
        if data.get('shares_outstanding') is None:
            outstanding_patterns = [
                # "shares in issue (excluding treasury)" — most precise, try first
                r'([\d,]+)\s+(?:ordinary\s+)?shares?\s+in\s+issue\s*\(?(?:excluding|excl)',
                # "shares in issue less ... treasury shares is X" (JMGI format)
                r'shares?\s+in\s+issue\s+less\s+.*?treasury\s+shares?\s+is\s+([\d,]+)',
                # "will have / has X shares in issue" — but NOT "(including treasury)"
                r'(?:will\s+have|shall\s+have|has)\s+([\d,]+)\s+(?:ordinary\s+)?shares?\s+(?:currently\s+)?in\s+issue(?!\s*\(?including)',
                r'([\d,]+)\s+(?:ordinary\s+)?shares?\s+currently\s+in\s+issue',
                # Generic "X shares in issue" — reject if followed by "(including treasury)"
                r'([\d,]+)\s+(?:ordinary\s+)?shares?\s+in\s+issue(?!\s*\(?including)',
                r'issued\s+(?:share\s+)?(?:ordinary\s+)?capital\s+(?:of|comprises?)\s+([\d,]+)',
                r'issued\s+ordinary\s+shares?\s*(?:\((?:excluding|excl).*?\))?\s*(?::|comprises?)\s*([\d,]+)',
            ]
            for pattern in outstanding_patterns:
                match = re.search(pattern, text_lower)
                if match:
                    os_str = match.group(1).replace(',', '')
                    try:
                        data['shares_outstanding'] = int(os_str)
                        break
                    except ValueError:
                        continue

        # Voting rights fallback from shares_outstanding
        if data.get('voting_rights') is None and data.get('shares_outstanding') is not None:
            data['voting_rights'] = data['shares_outstanding']

        # Treasury shares — patterns ordered from most specific to least
        if data.get('held_in_treasury') is None:
            treasury_patterns = [
                # "N shares/ordinary shares held in treasury" (direct adjacency)
                r'([\d,]+)\s+(?:ordinary\s+)?shares?\s+held\s+in\s+treasury',
                # "held in treasury is/are N" (label-value)
                r'held\s+(?:by\s+the\s+company\s+)?in\s+treasury\s+(?:is|are|of)\s+([\d,]+)',
                # "treasury shares: N" or "treasury: N"
                r'treasury\s+(?:shares?\s*)?(?::|is|are|of)\s*([\d,]+)',
                # "N held in treasury" (with short gap — max 30 chars to avoid grabbing purchase amounts)
                r'([\d,]+).{0,30}held\s+in\s+treasury',
            ]
            for pattern in treasury_patterns:
                match = re.search(pattern, text_lower)
                if match:
                    ts_str = match.group(1).replace(',', '')
                    try:
                        data['held_in_treasury'] = int(ts_str)
                        break
                    except ValueError:
                        continue

        # Effective date
        if data.get('effective_date') is None:
            data['effective_date'], data['_effective_date_confident'] = self._extract_effective_date(
                page_text, data.get('announcement_date'), ticker=data.get('ticker')
            )

        # Cancellation status
        if data.get('cancellation_status') is None:
            if 'cancelled' in text_lower:
                data['cancellation_status'] = 'Cancelled'
            elif data.get('held_in_treasury'):
                data['cancellation_status'] = 'Treasury'

    def _apply_ticker_overrides(self, data):
        """Apply ticker-specific post-processing rules."""
        ticker = data.get('ticker', '')
        overrides = config.TICKER_OVERRIDES.get(ticker, {})

        # CTY: divide shares by 15 (one vote per 15 shares)
        divisor = overrides.get('shares_divisor')
        if divisor and data.get('shares_transacted'):
            data['shares_transacted'] = data['shares_transacted'] // divisor

        # BHMG/BHMU: multiply shares and voting_rights by factor to convert to voting rights equivalent
        multiplier = overrides.get('shares_multiplier')
        if multiplier:
            if data.get('shares_transacted'):
                original = data['shares_transacted']
                data['shares_transacted'] = round(data['shares_transacted'] * multiplier)
                print(f"    ℹ {ticker}: shares_transacted × {multiplier} ({original} → {data['shares_transacted']})")
            if data.get('voting_rights'):
                original_vr = data['voting_rights']
                data['voting_rights'] = round(data['voting_rights'] * multiplier)
                print(f"    ℹ {ticker}: voting_rights × {multiplier} ({original_vr} → {data['voting_rights']})")

        # MNTN: price quoted in pence, output required in pounds — divide by 100
        price_divisor = overrides.get('price_divisor')
        if price_divisor and data.get('average_price'):
            data['average_price'] = round(data['average_price'] / price_divisor, 6)
            print(f"    ℹ {ticker}: price divided by {price_divisor} ({data['average_price']})")

        # CVCE: price in EUR, output required in euro cents — multiply by 100
        price_multiplier = overrides.get('price_multiplier')
        if price_multiplier and data.get('average_price'):
            original = data['average_price']
            data['average_price'] = round(data['average_price'] * price_multiplier, 6)
            print(f"    ℹ {ticker}: price × {price_multiplier} ({original} → {data['average_price']})")

        # Currency override: reverse ×100 conversion for non-pence currencies
        override_currency = overrides.get('currency')
        if override_currency and override_currency.lower() not in ('gbp', 'gbx', 'pence', 'p'):
            if data.get('_pounds_converted') and data.get('average_price'):
                data['average_price'] = round(data['average_price'] / 100, 6)
                data['currency'] = override_currency
                print(f"    ℹ {ticker}: reversed £→p conversion (currency={override_currency})")

    def _extract_weekly_aggregate(self, page_text):
        """Extract aggregated data from a multi-day transaction table.

        Looks for rows matching: date + number of shares + price per row.
        Returns dict with total_shares, vwap, last_date, row_count, or None.
        """
        # Pattern: date followed by shares and price on the same line or nearby.
        # Common formats in weekly tables:
        #   17 March 2026    5,000    1,570.50
        #   18/03/2026       3,000    1,574.20p
        #   27 April 2026129,0781809.00 pence1831.00 pence1816.15 pence
        date_part = r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}|\d{1,2}[/\-]\d{1,2}[/\-]\d{4})'
        rows = []

        # Five-column LSE aggregate tables include low, high, and VWAP prices.
        # Use the final price column as VWAP. The LSE API sometimes glues
        # columns together, so separators are intentionally optional.
        aggregate_pattern = re.compile(
            rf'{date_part}\s*([\d,\s]+\.\d{{2}})\s*(?:p(?:ence)?)?\s*'
            rf'([\d,]+\.\d{{2}})\s*(?:p(?:ence)?)?\s*'
            rf'([\d,]+\.\d{{2}})\s*(?:p(?:ence)?)?',
            re.IGNORECASE,
        )
        for date_str, shares_low_price, _high_price, vwap_price in aggregate_pattern.findall(page_text):
            try:
                share_price = self._split_joined_share_price(shares_low_price)
                if not share_price:
                    continue
                shares, _low_price = share_price
                price = float(vwap_price.replace(',', ''))
                if shares > 0 and price > 0:
                    rows.append({
                        'date': date_str,
                        'shares': shares,
                        'price': price,
                        'price_decimals': self._decimal_places(vwap_price),
                    })
            except (ValueError, ZeroDivisionError):
                continue

        if len(rows) < 2:
            rows = self._extract_vertical_weekly_rows(page_text, date_part)

        if len(rows) < 2:
            shares_part = r'([\d,]+)'
            price_part = r'([\d,.]+)\s*(?:p(?:ence)?)?'
            row_pattern = rf'{date_part}\s+{shares_part}\s+{price_part}'
            matches = re.findall(row_pattern, page_text, re.IGNORECASE)

            if len(matches) < 2:
                return None  # Not a multi-row table

            rows = []
            for date_str, shares_str, price_str in matches:
                try:
                    shares = int(shares_str.replace(',', ''))
                    price = float(price_str.replace(',', ''))
                    if shares > 0 and price > 0:
                        rows.append({
                            'date': date_str,
                            'shares': shares,
                            'price': price,
                            'price_decimals': self._decimal_places(price_str),
                        })
                except (ValueError, ZeroDivisionError):
                    continue

        if len(rows) < 2:
            return None

        for row in rows:
            try:
                if not row['date'] or not row['shares'] or not row['price']:
                    return None
            except (ValueError, ZeroDivisionError):
                return None

        total_shares = sum(r['shares'] for r in rows)
        weighted_sum = sum(r['shares'] * r['price'] for r in rows)
        price_decimals = max(2, min(6, max(r.get('price_decimals', 2) for r in rows)))
        vwap = round(weighted_sum / total_shares, price_decimals)
        last_date = self._normalize_date(rows[-1]['date'])

        return {
            'total_shares': total_shares,
            'vwap': vwap,
            'last_date': last_date,
            'row_count': len(rows),
        }

    def _split_joined_share_price(self, value):
        """Split a glued aggregate block such as 129,0781809.00."""
        compact = re.sub(r'\s+', '', str(value or ''))

        comma_grouped = re.match(r'^(\d{1,3}(?:,\d{3})+)(\d+(?:,\d{3})*\.\d{2})$', compact)
        if comma_grouped:
            shares = int(comma_grouped.group(1).replace(',', ''))
            price = float(comma_grouped.group(2).replace(',', ''))
            return shares, price

        if '.' not in compact:
            return None

        integer_part, decimal_part = compact.rsplit('.', 1)
        if len(decimal_part) != 2 or not integer_part.isdigit():
            return None

        max_price_digits = min(5, len(integer_part) - 1)
        for price_digits in range(max_price_digits, 1, -1):
            shares_part = integer_part[:-price_digits]
            price_integer = integer_part[-price_digits:]
            if not shares_part or not shares_part.isdigit():
                continue
            if len(price_integer) > 1 and price_integer.startswith('0'):
                continue

            shares = int(shares_part)
            price = float(f"{price_integer}.{decimal_part}")
            if shares > 0 and 0 < price < 20000:
                return shares, price

        return None

    def _extract_vertical_weekly_rows(self, page_text, date_part):
        """Extract weekly rows from vertical LSE tables grouped by field."""
        date_block = self._between_labels(
            page_text,
            r'Date\s+of\s+purchase\s*:',
            r'Number\s+of\s+shares\s+purchased\s*:'
        )
        shares_block = self._between_labels(
            page_text,
            r'Number\s+of\s+shares\s+purchased\s*:',
            r'Volume\s+weighted\s+average\s+price\s+paid\s+per\s+share'
        )
        price_block = self._between_labels(
            page_text,
            r'Volume\s+weighted\s+average\s+price\s+paid\s+per\s+share\s*(?:\([^)]+\))?\s*:',
            r'Highest\s+price\s+paid\s+per\s+share'
        )
        if not date_block or not shares_block or not price_block:
            return []

        dates = re.findall(date_part, date_block, re.IGNORECASE)
        shares = [
            int(match.replace(',', ''))
            for match in re.findall(r'\b\d{1,3}(?:,\d{3})+\b|\b\d+\b', shares_block)
        ]
        price_strings = re.findall(r'\b\d+(?:,\d{3})*(?:\.\d+)?\b', price_block)
        prices = [float(match.replace(',', '')) for match in price_strings]

        if not (len(dates) == len(shares) == len(prices)) or len(dates) < 2:
            return []

        rows = []
        for date_str, share_count, price, price_str in zip(dates, shares, prices, price_strings):
            if share_count > 0 and price > 0:
                rows.append({
                    'date': date_str,
                    'shares': share_count,
                    'price': price,
                    'price_decimals': self._decimal_places(price_str),
                })
        return rows

    @staticmethod
    def _between_labels(text, start_pattern, end_pattern):
        match = re.search(
            rf'{start_pattern}\s*(.*?)(?={end_pattern})',
            text,
            re.IGNORECASE | re.DOTALL,
        )
        return match.group(1) if match else None

    @staticmethod
    def _decimal_places(value):
        text = str(value or '').replace(',', '')
        match = re.search(r'\.(\d+)', text)
        return len(match.group(1)) if match else 0

    def _sanity_check(self, data, page_text=None):
        """Validate extracted values, clearing obviously wrong ones."""
        shares = data.get('shares_transacted')
        voting = data.get('voting_rights')
        outstanding = data.get('shares_outstanding')

        # Shares can't exceed voting rights or outstanding
        if shares and voting and shares > voting:
            print(f"    ⚠ Shares ({shares}) > voting rights ({voting}) — clearing shares")
            data['shares_transacted'] = None
        if shares and outstanding and shares > outstanding:
            print(f"    ⚠ Shares ({shares}) > outstanding ({outstanding}) — clearing shares")
            data['shares_transacted'] = None

        # Daily buyback sanity cap
        if shares and shares > config.MAX_DAILY_SHARES:
            print(f"    ⚠ Shares ({shares}) > {config.MAX_DAILY_SHARES} cap — clearing")
            data['shares_transacted'] = None

        # Cross-validation: if shares == treasury, we almost certainly grabbed the wrong number
        treasury = data.get('held_in_treasury')
        if shares and treasury and shares == treasury:
            print(f"    ⚠ Shares ({shares}) == held_in_treasury ({treasury}) — likely grabbed treasury total, clearing")
            data['shares_transacted'] = None

        # Multi-transaction detection
        if page_text:
            multi = re.findall(
                r'(?:purchased|bought)\s+[\d,]+\s+shares?\s+at\s+(?:an\s+)?(?:average\s+)?(?:price\s+of\s+)?',
                page_text.lower()
            )
            if len(multi) > 1:
                print(f"    ⚠ Multi-transaction detected ({len(multi)} transactions)")
                data['_multi_transaction'] = True

    @staticmethod
    def _normalize_date(date_str):
        """Normalize date to 'DD Month YYYY' format (internal representation)."""
        if not date_str:
            return date_str
        # DD Month YYYY
        match = re.match(r'^(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})$', date_str)
        if match:
            return f"{int(match.group(1)):02d} {match.group(2)} {match.group(3)}"
        # DD/MM/YYYY
        match = re.match(r'^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$', date_str)
        if match:
            try:
                dt = datetime.strptime(f"{match.group(1)}/{match.group(2)}/{match.group(3)}", "%d/%m/%Y")
                return dt.strftime("%d %B %Y")
            except ValueError:
                pass
        # YYYY-MM-DD
        match = re.match(r'^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$', date_str)
        if match:
            try:
                dt = datetime.strptime(f"{match.group(1)}-{match.group(2)}-{match.group(3)}", "%Y-%m-%d")
                return dt.strftime("%d %B %Y")
            except ValueError:
                pass
        return date_str

    @staticmethod
    def normalize_date_for_output(date_str):
        """Convert any date format to 'YYYY-MM-DD 00:00:00' for SQL import."""
        if not date_str:
            return None
        # DD Month YYYY
        match = re.match(r'^(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})$', date_str)
        if match:
            try:
                dt = datetime.strptime(f"{match.group(1)} {match.group(2)} {match.group(3)}", "%d %B %Y")
                return dt.strftime("%Y-%m-%d 00:00:00")
            except ValueError:
                pass
        # DD/MM/YYYY
        match = re.match(r'^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$', date_str)
        if match:
            try:
                dt = datetime.strptime(f"{match.group(1)}/{match.group(2)}/{match.group(3)}", "%d/%m/%Y")
                return dt.strftime("%Y-%m-%d 00:00:00")
            except ValueError:
                pass
        # YYYY-MM-DD (already close to target)
        match = re.match(r'^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$', date_str)
        if match:
            try:
                dt = datetime.strptime(f"{match.group(1)}-{match.group(2)}-{match.group(3)}", "%Y-%m-%d")
                return dt.strftime("%Y-%m-%d 00:00:00")
            except ValueError:
                pass
        return date_str

    @staticmethod
    def parse_date_to_datetime(date_str):
        """Convert any date string to a Python datetime object.

        Supports: 'DD Month YYYY', 'DD/MM/YYYY', 'YYYY-MM-DD',
                  'YYYY-MM-DD 00:00:00'.
        Returns None if parsing fails.
        """
        if not date_str:
            return None
        if isinstance(date_str, datetime):
            return date_str
        date_str = str(date_str).strip()
        # Strip trailing time component (e.g. '2026-03-16 00:00:00')
        date_str = re.sub(r'\s+\d{2}:\d{2}:\d{2}$', '', date_str)
        formats = [
            ('%d %B %Y',),     # 16 March 2026
            ('%d/%m/%Y',),     # 16/03/2026
            ('%d-%m-%Y',),     # 16-03-2026
            ('%Y-%m-%d',),     # 2026-03-16
            ('%Y/%m/%d',),     # 2026/03/16
        ]
        for fmt_tuple in formats:
            try:
                return datetime.strptime(date_str, fmt_tuple[0])
            except ValueError:
                continue
        return None

    @staticmethod
    def assess_quality(data):
        """Assess data quality, returning comma-separated issue codes or 'ok'."""
        issues = []
        if data.get('average_price') is None:
            issues.append('no_price')
        elif data['average_price'] > config.PRICE_TOO_HIGH:
            issues.append('price_too_high')
        elif data['average_price'] < config.PRICE_TOO_LOW:
            issues.append('price_too_low')
        if data.get('shares_transacted') is None:
            issues.append('no_shares')
        if data.get('transaction_type') is None:
            issues.append('no_type')
        if data.get('shares_outstanding') is None and data.get('voting_rights') is None:
            issues.append('no_outstanding')
        return ','.join(issues) if issues else 'ok'
