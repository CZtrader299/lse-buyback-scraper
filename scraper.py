"""LSE Buyback Scraper with optional AI Review - Main Entry Point.

Orchestrates the full pipeline:
1. Scrape LSE News Explorer for buyback/issuance announcements
2. Extract data using regex (primary) + optional AI reviewer (fallback)
3. Validate against prior day's data (optional)
4. Output Excel in SQL import format with confidence highlighting

Usage:
    python scraper.py
    python scraper.py --prior-day "path/to/prior.xlsx"
    python scraper.py --no-ai
    python scraper.py --ai-provider ollama
    python scraper.py --ai-provider claude_cli
"""

import argparse
import glob
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Fix Windows console encoding
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import config
from browser import LSEBrowser
import lse_api
from extractor import Extractor
from ai_reviewer import AIReviewer
from reconciler import Reconciler
from output_writer import OutputWriter


# Keywords that indicate an announcement page has loaded its financial content
_ANNOUNCEMENT_KEYWORDS = ('shares', 'ordinary', 'transaction', 'voting rights',
                          'share capital', 'treasury', 'buyback', 'purchase')


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description='LSE Buyback Scraper with optional AI Review')
    parser.add_argument('--prior-day', type=str, default=None,
                       help='Path to prior day output file for day-over-day validation')
    parser.add_argument('--no-claude', action='store_true',
                       help='Backward-compatible alias for --no-ai')
    parser.add_argument('--no-ai', action='store_true',
                       help='Skip AI review (regex only)')
    parser.add_argument('--ai-provider',
                       choices=['none', 'auto', 'ollama', 'claude_cli', 'anthropic_api', 'openai_api'],
                       default=None,
                       help='AI review provider. If omitted, prompts interactively.')
    parser.add_argument('--headless', action='store_true',
                       help='Run browser in headless mode')
    parser.add_argument('--demo', action='store_true',
                       help='Run an offline demo against bundled tests/fixtures/*.txt '
                            '(no Selenium, no LSE network access)')
    return parser.parse_args()


def _choose_ai_provider(default_provider=None):
    """Ask the user which AI reviewer provider to use for this run."""
    default_provider = (default_provider or config.AI_REVIEW_PROVIDER or 'auto').strip().lower()
    if default_provider == 'claude':
        default_provider = 'claude_cli'
    valid = ('auto', 'anthropic_api', 'openai_api', 'claude_cli', 'ollama', 'none')
    if default_provider not in valid:
        default_provider = 'auto'

    options = {
        '1': ('auto', 'Auto-detect (best available)'),
        '2': ('anthropic_api', 'Anthropic API (ANTHROPIC_API_KEY)'),
        '3': ('openai_api', 'OpenAI API (OPENAI_API_KEY)'),
        '4': ('claude_cli', 'Claude CLI (local install)'),
        '5': ('ollama', 'Ollama local'),
        '6': ('none', 'Regex only / no AI'),
    }
    default_number = next(
        (number for number, (provider, _label) in options.items() if provider == default_provider),
        '1',
    )

    print("Choose AI reviewer for this run:")
    for number, (provider, label) in options.items():
        suffix = " [default]" if number == default_number else ""
        print(f"  {number}. {label}{suffix}")

    try:
        choice = input(f"Selection ({default_number}): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        choice = ''

    if not choice:
        choice = default_number

    if choice not in options:
        print(f"Unknown selection '{choice}', using {options[default_number][1]}.")
        choice = default_number

    provider, label = options[choice]
    print(f"Using AI reviewer: {label}\n")
    return provider


def _detect_replacement(page_text):
    """Check if announcement is a replacement/correction of a prior announcement.

    Returns True if replacement language appears near the top of the text.
    """
    # Check first 500 chars for replacement indicators
    header = page_text[:500].lower()
    replacement_phrases = (
        'replacement',
        'replaces announcement',
        'this announcement replaces',
        'corrected version',
        'amended version',
        'correction of',
        'this replaces',
    )
    return any(phrase in header for phrase in replacement_phrases)


def _detect_non_standard(data, page_text):
    """Check if announcement is non-standard (e.g. weekly summary, programme update).

    Returns a brief description string if non-standard, None otherwise.
    """
    # If extraction found at least one key field, it's standard enough
    if data.get('shares_transacted') is not None or data.get('average_price') is not None:
        return None

    text_lower = page_text[:2000].lower()

    # Weekly/periodic summary patterns
    if any(phrase in text_lower for phrase in ('weekly summary', 'weekly report', 'weekly update',
                                                'periodic summary', 'programme update',
                                                'total for the week', 'summary of transactions')):
        return "Weekly/periodic summary â€” not a single transaction"

    # Programme announcement (no actual transaction)
    if any(phrase in text_lower for phrase in ('programme announcement', 'buyback programme',
                                                'share buyback program', 'mandate to purchase')):
        if not any(phrase in text_lower for phrase in ('purchased', 'repurchased', 'bought back')):
            return "Programme announcement â€” no transaction data"

    # If all key fields are missing and we got here, flag generically
    if (data.get('shares_transacted') is None and data.get('average_price') is None
            and data.get('voting_rights') is None):
        return "Non-standard announcement â€” no transaction fields extracted"

    return None


# Regex patterns for extracting ticker/TIDM from announcement body text.
# These fields appear in RNS headers, typically within the first 1500 chars.
_TIDM_RE = re.compile(
    r'(?:TIDM|EPIC(?:/TIDM)?|Ticker(?:\s*Symbol)?)\s*[:=]\s*([A-Z][A-Z0-9.]{0,9})',
    re.IGNORECASE,
)

# Fallback: many RNS announcements use "Company Name Limited (TICKER)" format
# in the header, e.g. "The Schiehallion Fund Limited (MNTN)"
_PARENS_TICKER_RE = re.compile(
    r'(?:Limited|Ltd|plc|PLC|Inc|Corporation|Corp|Trust|Fund)\s*\(([A-Z][A-Z0-9]{1,9})\)',
)


def _detect_transacted_share_classes(page_text):
    """Detect which share classes are being transacted in the announcement narrative.

    Searches only the transaction narrative (first 2000 chars) to avoid false
    positives from the voting rights / shares-outstanding section, which always
    lists balances for every share class regardless of which class was transacted.

    Looks for patterns like:
      - "allotted 100,000 Income shares"
      - "purchased 50,000 Growth shares of Â£X each"
      - "per Income share" (in the price description)

    Returns:
        set of lowercase class keywords found (e.g. {'income'}, {'growth'},
        {'income', 'growth'} when both classes were transacted).
        Empty set if no class-specific transaction language is found.
    """
    if not page_text:
        return set()

    narrative = page_text[:2000].lower()
    found = set()

    for class_kw in ('income', 'growth'):
        # "allotted/purchased/bought back/repurchased/issued N <class> shares"
        if re.search(
            rf'(?:allotted?|purchased?|bought\s+back|repurchased?|issued?)\s+[\d,]+\s+{class_kw}\s+shares?',
            narrative,
        ):
            found.add(class_kw)
            continue
        # "per <class> share" â€” appears in the price-per-share description
        if re.search(rf'\bper\s+{class_kw}\s+share\b', narrative):
            found.add(class_kw)
            continue
        # "N <class> shares of Â£X each" â€” quantity + class in share capital description
        if re.search(rf'[\d,]+\s+{class_kw}\s+shares?\s+of\s+[Â£â‚¬\$]', narrative):
            found.add(class_kw)

    return found


def resolve_ticker_from_text(page_text):
    """Extract ticker symbol from announcement body text.

    Searches the first 1500 characters for TIDM/EPIC/Ticker fields
    commonly found in RNS announcement headers.  Falls back to matching
    ticker symbols in parentheses after company-name suffixes like
    "Limited", "plc", "Trust", etc.

    Args:
        page_text: Full announcement text

    Returns:
        Uppercase ticker string, or None if not found.
    """
    if not page_text:
        return None
    header = page_text[:1500]
    match = _TIDM_RE.search(header)
    if match:
        return match.group(1).upper()
    match = _PARENS_TICKER_RE.search(header)
    if match:
        return match.group(1).upper()
    return None


def _process_announcement(announcement, page_text, extractor, reviewer, results, all_highlights, stats):
    """Process a single announcement through extraction + AI review pipeline.

    Handles manual-only tickers, replacement detection, non-standard detection,
    multi-class tickers, tier-1 regex extraction, tier-2 AI review,
    ticker overrides, and quality assessment. Appends results and highlights in-place.

    Args:
        announcement: dict with 'ticker', 'url', 'title' keys
        page_text: Full announcement text (already validated as non-stale)
        extractor: Extractor instance
        reviewer: AIReviewer instance (or None if --no-ai)
        results: list to append extracted data dicts to
        all_highlights: dict to store per-ticker highlight info
        stats: dict with 'regex_ok', 'claude_filled', 'claude_disagreed', 'failed' counters
    """
    ticker = announcement['ticker']
    url = announcement['url']

    # --- Early exit: Manual-only tickers (skip all extraction) ---
    manual_only = getattr(config, 'MANUAL_ONLY_TICKERS', {})
    if ticker in manual_only:
        reason = manual_only[ticker]
        print(f"  âš  {ticker}: manual-only ticker â€” {reason}")
        results.append({
            'ticker': ticker,
            'transaction_type': None,
            'shares_transacted': None,
            'average_price': None,
            'currency': None,
            'announcement_date': None,
            'effective_date': None,
            'voting_rights': None,
            'shares_outstanding': None,
            'held_in_treasury': None,
            'cancellation_status': None,
            'extraction_method': f'FLAGGED â€” {reason}',
            'url': url,
            'page_text': page_text,
        })
        all_highlights[ticker] = {'claude_filled': [], 'disagreement': [], 'agreed': []}
        stats['failed'] += 1
        return

    # --- Early exit: Replacement/correction announcements ---
    if _detect_replacement(page_text):
        print(f"  âš  {ticker}: replacement/correction announcement â€” flagging for review")
        results.append({
            'ticker': ticker,
            'transaction_type': None,
            'shares_transacted': None,
            'average_price': None,
            'currency': None,
            'announcement_date': None,
            'effective_date': None,
            'voting_rights': None,
            'shares_outstanding': None,
            'held_in_treasury': None,
            'cancellation_status': None,
            'extraction_method': 'FLAGGED â€” Replacement announcement',
            'url': url,
            'page_text': page_text,
        })
        all_highlights[ticker] = {'claude_filled': [], 'disagreement': [], 'agreed': []}
        stats['failed'] += 1
        return

    # --- Share class filter: sibling tickers sharing the same LSE announcement ---
    # e.g. CMPI (Income) and CMPG (Growth) both receive the same RNS URL.
    # Skip this ticker if the transaction narrative mentions only the sibling's class.
    share_class_tickers = getattr(config, 'SHARE_CLASS_TICKERS', {})
    if ticker in share_class_tickers:
        class_cfg = share_class_tickers[ticker]
        class_kw = class_cfg['class_keyword']
        sibling = class_cfg['sibling']
        transacted = _detect_transacted_share_classes(page_text)
        if transacted and class_kw not in transacted:
            print(f"  â„¹ {ticker}: announcement is for {', '.join(sorted(transacted))} shares only "
                  f"â€” skipping (will be captured under {sibling})")
            return

    # Multi-class handling: tickers with multiple share classes (e.g. HAN/HANA)
    multi_class_tickers = getattr(config, 'MULTI_CLASS_TICKERS', {})
    if ticker in multi_class_tickers and reviewer and reviewer.available:
        class_tickers = multi_class_tickers[ticker]
        print(f"  â†’ Multi-class ticker ({', '.join(class_tickers)}) â€” sending to AI reviewer")
        class_results = reviewer.review_multi_class(page_text, ticker, class_tickers)
        if class_results:
            added_any = False
            for cls in class_results:
                # Skip empty rows (no transaction for this share class)
                if cls.get('shares_transacted') is None and cls.get('price_pence') is None:
                    print(f"    â†’ {cls.get('ticker', '?')}: no transaction data â€” skipping row")
                    continue
                class_ticker = cls.get('ticker', ticker)
                regex_baseline = extractor.extract(page_text, class_ticker)
                price_pence = reviewer._normalise_ai_price(
                    cls.get('price_pence'),
                    regex_baseline.get('average_price'),
                    cls,
                )
                class_data = {
                    'ticker': class_ticker,
                    'transaction_type': cls.get('event_type'),
                    'shares_transacted': cls.get('shares_transacted'),
                    'average_price': price_pence,
                    'currency': cls.get('currency', 'GBp'),
                    'announcement_date': cls.get('announce_date'),
                    'effective_date': cls.get('transaction_date'),
                    'voting_rights': cls.get('shares_in_issue'),
                    'shares_outstanding': None,
                    'held_in_treasury': None,
                    'cancellation_status': None,
                    'extraction_method': 'claude_multi_class',
                    'url': url,
                    'page_text': page_text,
                }
                extractor._apply_ticker_overrides(class_data)
                results.append(class_data)
                all_highlights[class_ticker] = {'claude_filled': [], 'disagreement': [], 'agreed': []}
                added_any = True
                print(f"    â†’ {class_ticker}: {cls.get('event_type')} | "
                      f"Shares: {cls.get('shares_transacted')} | "
                      f"Price: {price_pence}")
            if added_any:
                stats['claude_filled'] += 1
                reviewer.update_history(ticker, needed_claude=True)
                return
            else:
                print(f"  âš  All share classes empty â€” falling back to single-row")
        else:
            print(f"  âš  Multi-class extraction failed â€” falling back to single-row")

    # Tier 1: Regex extraction
    data = extractor.extract(page_text, ticker)
    data['url'] = url
    data['page_text'] = page_text

    # Announcement date is always today â€” the news explorer only shows same-day
    # announcements, so the regex-extracted date from the text is unreliable.
    data['announcement_date'] = datetime.now().strftime('%d %B %Y')

    # Multi-class regex fallback: if this is a multi-class ticker and AI review
    # wasn't used (or failed), detect share class from text and remap ticker.
    # e.g. HAN announcement mentioning "A non-voting" shares â†’ ticker becomes HANA
    if ticker in multi_class_tickers:
        _a_share_patterns = (
            r'\ba\s+non[- ]?voting\b',
            r'\ba\s+ordinary\s+shares?\b',
            r'\bclass\s+a\s+shares?\b',
        )
        text_lower = page_text.lower()
        is_a_shares = any(re.search(p, text_lower) for p in _a_share_patterns)
        if is_a_shares and 'HANA' in multi_class_tickers.get(ticker, []):
            data['ticker'] = 'HANA'
            ticker = 'HANA'
            print(f"  â„¹ Detected A non-voting shares â€” remapped ticker to HANA")

    # Tier 2: AI review (if enabled)
    highlights = {'claude_filled': [], 'disagreement': [], 'agreed': []}
    claude_data = None
    regex_ok_incremented = False

    # Skip AI review for NOT_TRACKED tickers (saves reviewer time)
    not_tracked = ticker in getattr(config, 'NOT_TRACKED_TICKERS', [])
    if not_tracked:
        data['_not_tracked'] = True
        print(f"  â„¹ {ticker} is not tracked â€” skipping AI review")

    if reviewer and reviewer.available and not not_tracked:
        should_review, reason = reviewer.should_review(data, ticker)
        if should_review:
            print(f"  â†’ Sending to AI reviewer (reason: {reason})")
            claude_data = reviewer.review(page_text, ticker)

            if claude_data:
                data, highlights = reviewer.merge(data, claude_data)
                if highlights['claude_filled']:
                    print(f"    âœ“ AI reviewer filled: {highlights['claude_filled']}")
                    stats['claude_filled'] += 1
                if highlights['disagreement']:
                    print(f"    âš  AI reviewer disagreed: {highlights['disagreement']}")
                    stats['claude_disagreed'] += 1

            # AI reviewer was invoked = regex was insufficient, regardless of result
            reviewer.update_history(ticker, needed_claude=True)
        else:
            reviewer.update_history(ticker, needed_claude=False)
            stats['regex_ok'] += 1
            regex_ok_incremented = True
    else:
        stats['regex_ok'] += 1
        regex_ok_incremented = True

    # Auto-learn: track agreements for pattern promotion
    if claude_data and getattr(config, 'AUTO_LEARN_ENABLED', False):
        if highlights['agreed'] and not highlights['disagreement']:
            agreed_snapshot = {}
            for field in highlights['agreed']:
                agreed_snapshot[field] = data.get(field)
            promote_result = reviewer.record_agreement(ticker, agreed_snapshot)
            if promote_result == 'promote' and extractor.patterns_db:
                from claude_reviewer import extract_label_pattern
                new_patterns = {}
                field_to_pattern = {
                    'average_price': ('price_start', 'price_end', 'price'),
                    'shares_transacted': ('shares_start', 'shares_end', 'shares'),
                    'voting_rights': ('voting_rights_start', 'voting_rights_end', 'voting_rights'),
                }
                for field, (start_key, end_key, ftype) in field_to_pattern.items():
                    val = data.get(field)
                    if val is not None:
                        val_str = f"{val:,}" if isinstance(val, int) else f"{val:,.2f}" if isinstance(val, float) and val >= 1000 else str(val)
                        pat = extract_label_pattern(page_text, val_str, field_type=ftype)
                        if pat:
                            new_patterns[start_key] = pat['start']
                            new_patterns[end_key] = pat['end']
                if new_patterns:
                    extractor.patterns_db.write_pattern(ticker, new_patterns)
        elif highlights['disagreement']:
            reviewer.record_disagreement(ticker)

    # --- Post-extraction: detect non-standard announcements ---
    non_standard = _detect_non_standard(data, page_text)
    if non_standard:
        print(f"  âš  {ticker}: {non_standard}")
        data['extraction_method'] = f'FLAGGED â€” {non_standard}'
        stats['failed'] += 1
        if regex_ok_incremented:
            stats['regex_ok'] -= 1  # Undo this announcement's regex_ok count

    all_highlights[ticker] = highlights
    results.append(data)

    # Print summary
    print(f"  Result: {data.get('transaction_type')} | "
          f"Shares: {data.get('shares_transacted')} | "
          f"Price: {data.get('average_price')} | "
          f"Quality: {data.get('_data_quality')}")


def run_demo_mode(args, ai_provider):
    """Offline demo: run extraction (and optionally AI review) against bundled fixtures.

    Reads every tests/fixtures/*.txt file, infers a ticker from the filename, and runs
    each one through the extractor and (if available) the AI reviewer. No Selenium and
    no LSE network calls are made. Results are written to lse_transactions_demo_<ts>.xlsx.
    """
    print("=" * 70)
    print("LSE Buyback Scraper — DEMO MODE (offline, fixtures only)")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    fixtures_dir = os.path.join(script_dir, 'tests', 'fixtures')
    if not os.path.isdir(fixtures_dir):
        print(f"  ✗ No fixtures directory found at {fixtures_dir}")
        return

    fixture_paths = sorted(glob.glob(os.path.join(fixtures_dir, '*.txt')))
    if not fixture_paths:
        print(f"  ✗ No fixture *.txt files found in {fixtures_dir}")
        return

    print(f"  Found {len(fixture_paths)} fixture(s).")

    extractor = Extractor()
    reviewer = None if (args.no_ai or args.no_claude) else AIReviewer(ai_provider)

    results = []
    all_highlights = {}
    stats = {'regex_ok': 0, 'claude_filled': 0, 'claude_disagreed': 0,
             'failed': 0, 'resolved_tickers': 0}

    for i, path in enumerate(fixture_paths, 1):
        filename = os.path.basename(path)
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            page_text = f.read()

        # Infer ticker: filename pattern sample_announcement_<ticker>.txt, else from text
        ticker = None
        m = re.match(r'sample_announcement_([a-zA-Z0-9]+)\.txt$', filename)
        if m:
            ticker = m.group(1).upper()
        if not ticker:
            ticker = resolve_ticker_from_text(page_text)
        if not ticker:
            ticker = 'UNKNOWN'

        print(f"\n[{i}/{len(fixture_paths)}] {filename} → {ticker}")
        print(f"  Loaded {len(page_text)} chars")

        announcement = {
            'ticker': ticker,
            'url': f'file://{path}',
            'title': filename,
            'article_id': None,
        }

        _process_announcement(announcement, page_text, extractor, reviewer,
                              results, all_highlights, stats)

    # Summary table
    print("\n" + "=" * 70)
    print("DEMO RESULTS")
    print("=" * 70)
    print(f"{'Ticker':<10} {'Type':<10} {'Shares':>12} {'Price':>10} {'Method':<20}")
    print("-" * 70)
    for r in results:
        print(f"{str(r.get('ticker','?')):<10} "
              f"{str(r.get('transaction_type','-')):<10} "
              f"{str(r.get('shares_transacted','-')):>12} "
              f"{str(r.get('average_price','-')):>10} "
              f"{str(r.get('extraction_method','-')):<20}")

    # Write Excel
    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = os.path.join(os.getcwd(), f'lse_transactions_demo_{timestamp}.xlsx')
        OutputWriter.write(results, output_path, highlights=all_highlights, validations={})
        print(f"\nOutput: {output_path}")
    except Exception as exc:
        print(f"\n  ⚠ Could not write Excel output: {exc}")

    print(f"\nRegex OK: {stats['regex_ok']} | AI filled: {stats['claude_filled']} | "
          f"AI disagreed: {stats['claude_disagreed']} | Failed: {stats['failed']}")
    print("=" * 70)


def main():
    args = parse_args()

    ai_provider = None
    if not (args.no_ai or args.no_claude):
        ai_provider = args.ai_provider or _choose_ai_provider()

    if args.demo:
        run_demo_mode(args, ai_provider)
        return

    if args.headless:
        config.HEADLESS = True

    print("=" * 70)
    print("LSE Buyback Scraper with optional AI Review")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # Initialize components
    script_dir = os.path.dirname(os.path.abspath(__file__))
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # Set up file logging (tee to console + file)
    log_tee = None
    err_tee = None
    if config.LOG_TO_FILE:
        from logger import TeeLogger
        log_path = os.path.join(script_dir, f'lse_transactions_{timestamp}.log')
        log_tee = TeeLogger(log_path)
        err_tee = TeeLogger(log_path, stream=sys.__stderr__)
        sys.stdout = log_tee
        sys.stderr = err_tee

    try:
        extractor = Extractor()
        reviewer = None if (args.no_ai or args.no_claude) else AIReviewer(ai_provider)
        browser = None

        # Load prior day data if provided
        prior_shares = {}
        if args.prior_day:
            print(f"\nLoading prior day data from: {args.prior_day}")
            prior_shares = Reconciler.load_prior_day(args.prior_day)

        try:
            # Phase 1: SCRAPE
            print("\n" + "=" * 70)
            print("PHASE 1: Scraping LSE News Explorer")
            print("=" * 70)

            browser = LSEBrowser()
            announcements = browser.extract_all_links_with_pagination()
            if not announcements:
                print("âœ— No announcements found. Exiting.")
                return

            # Scraping health check
            if len(announcements) < 10:
                print(f"\nâš  WARNING: Only {len(announcements)} announcements found.")
                print("  This may indicate LSE website changes have broken selectors.")
                print("  Continuing with available data...\n")

            # Phase 2: EXTRACT
            print("\n" + "=" * 70)
            print("PHASE 2: Extracting transaction data")
            print("=" * 70)

            results = []
            all_highlights = {}
            stats = {'regex_ok': 0, 'claude_filled': 0, 'claude_disagreed': 0, 'failed': 0, 'resolved_tickers': 0}
            stale_tickers = []  # Announcements whose content isn't populated yet

            for i, announcement in enumerate(announcements, 1):
                ticker = announcement['ticker']
                url = announcement['url']
                title = announcement.get('title', '')

                if ticker:
                    print(f"\n[{i}/{len(announcements)}] {ticker}: {title[:60]}...")
                else:
                    print(f"\n[{i}/{len(announcements)}] [unknown ticker]: {title[:60]}...")

                # Get announcement text â€” API first, Selenium fallback
                article_id = announcement['article_id']
                page_text = lse_api.fetch_announcement_text(article_id)
                if page_text:
                    print(f"  API: fetched {len(page_text)} chars")
                else:
                    print(f"  API: no content, falling back to browser...")
                    page_text = browser.get_announcement_text(url)
                if not page_text:
                    print(f"  âœ— Could not extract text for {ticker or 'unknown'}")
                    stats['failed'] += 1
                    results.append({
                        'ticker': ticker or 'UNKNOWN',
                        'transaction_type': None,
                        'shares_transacted': None,
                        'average_price': None,
                        'currency': None,
                        'announcement_date': None,
                        'effective_date': None,
                        'voting_rights': None,
                        'shares_outstanding': None,
                        'held_in_treasury': None,
                        'cancellation_status': None,
                        'extraction_method': 'FAILED â€” page load failed',
                        'url': url,
                        'page_text': None,
                    })
                    all_highlights[ticker or 'UNKNOWN'] = {'claude_filled': [], 'disagreement': [], 'agreed': []}
                    continue

                # --- Ticker resolution for non-standard URLs ---
                if ticker is None:
                    print(f"  â†’ Resolving ticker from announcement text...")
                    ticker = resolve_ticker_from_text(page_text)
                    if ticker:
                        print(f"  âœ“ Resolved ticker from text: {ticker}")
                    elif reviewer and reviewer.available:
                        print(f"  â†’ Regex could not find ticker, sending to AI reviewer...")
                        ticker = reviewer.identify_ticker(page_text)
                    if ticker:
                        announcement['ticker'] = ticker
                        stats['resolved_tickers'] += 1
                    else:
                        print(f"  âœ— Could not resolve ticker â€” flagging for manual review")
                        ticker = 'UNKNOWN'
                        announcement['ticker'] = ticker
                        results.append({
                            'ticker': ticker,
                            'transaction_type': None,
                            'shares_transacted': None,
                            'average_price': None,
                            'currency': None,
                            'announcement_date': None,
                            'effective_date': None,
                            'voting_rights': None,
                            'shares_outstanding': None,
                            'held_in_treasury': None,
                            'cancellation_status': None,
                            'extraction_method': 'FLAGGED â€” Could not resolve ticker from non-standard URL',
                            'url': url,
                            'page_text': page_text,
                        })
                        all_highlights[ticker] = {'claude_filled': [], 'disagreement': [], 'agreed': []}
                        stats['failed'] += 1
                        continue

                # Content validation: check the page contains financial announcement
                # content rather than an error page or unrelated content.
                # Note: ticker symbols do NOT appear in announcement body text â€”
                # they are only in the URL â€” so we check for financial keywords instead.
                page_lower = page_text[:3000].lower()
                keyword_found = any(kw in page_lower for kw in _ANNOUNCEMENT_KEYWORDS)
                if not keyword_found:
                    print(f"  âš  Content not yet available for {ticker} (no financial keywords found)")
                    stale_tickers.append(announcement)
                    stats['failed'] += 1
                    # Stub row added now; will be replaced if retry succeeds
                    results.append({
                        'ticker': ticker,
                        'transaction_type': None,
                        'shares_transacted': None,
                        'average_price': None,
                        'currency': None,
                        'announcement_date': None,
                        'effective_date': None,
                        'voting_rights': None,
                        'shares_outstanding': None,
                        'held_in_treasury': None,
                        'cancellation_status': None,
                        'extraction_method': 'FAILED â€” stale content',
                        'url': url,
                        'page_text': None,
                    })
                    all_highlights[ticker] = {'claude_filled': [], 'disagreement': [], 'agreed': []}
                    continue

                print(f"  âœ“ Extracted {len(page_text)} characters")
                snippet = page_text[:200].replace('\n', ' ')
                print(f"  Text preview: {snippet}...")

                _process_announcement(announcement, page_text, extractor, reviewer,
                                      results, all_highlights, stats)

                # Rate limiting
                time.sleep(config.API_REQUEST_DELAY)

            # Phase 2b: RETRY stale tickers
            for retry_attempt in range(1, config.STALE_RETRY_MAX_ATTEMPTS + 1):
                if not stale_tickers:
                    break

                stale_names = [a['ticker'] for a in stale_tickers]
                print(f"\n{'=' * 70}")
                print(f"RETRY {retry_attempt}/{config.STALE_RETRY_MAX_ATTEMPTS}: "
                      f"{len(stale_tickers)} stale tickers: {', '.join(stale_names)}")
                print(f"Waiting {config.STALE_RETRY_DELAY}s for content to become available...")
                print(f"{'=' * 70}")
                time.sleep(config.STALE_RETRY_DELAY)

                still_stale = []
                for announcement in stale_tickers:
                    ticker = announcement['ticker']
                    url = announcement['url']
                    print(f"\n  [RETRY] {ticker}...")

                    article_id = announcement['article_id']
                    page_text = lse_api.fetch_announcement_text(article_id)
                    if not page_text:
                        try:
                            page_text = browser.get_announcement_text(url)
                        except Exception as e:
                            print(f"    âœ— Browser error for {ticker}: {e}")
                            still_stale.append(announcement)
                            continue
                    if not page_text:
                        print(f"    âœ— Still no text for {ticker}")
                        still_stale.append(announcement)
                        continue

                    page_lower = page_text[:3000].lower()
                    keyword_found = any(kw in page_lower for kw in _ANNOUNCEMENT_KEYWORDS)
                    if not keyword_found:
                        print(f"    âš  Still stale for {ticker}")
                        still_stale.append(announcement)
                        continue

                    print(f"    âœ“ Content now available ({len(page_text)} chars)")
                    stats['failed'] -= 1

                    # Remove the stub row we added earlier for this ticker
                    results[:] = [r for r in results if r.get('ticker') != ticker
                                  or r.get('extraction_method') != 'FAILED â€” stale content']

                    _process_announcement(announcement, page_text, extractor, reviewer,
                                          results, all_highlights, stats)
                    time.sleep(config.REQUEST_DELAY)

                stale_tickers = still_stale

        finally:
            if browser:
                browser.close()

        if not results:
            print("\nâœ— No results extracted. Exiting.")
            return

        # Filter out Conversion transaction types (spec: only Buyback and Issuance)
        results = [r for r in results if r.get('transaction_type') in ('Buyback', 'Issuance', None)]

        # Flag same-ticker-same-day duplicates for manual review (never drop rows)
        ticker_day_count = {}
        for r in results:
            key = (r.get('ticker'), r.get('announcement_date'))
            ticker_day_count[key] = ticker_day_count.get(key, 0) + 1
        for key, count in ticker_day_count.items():
            if count > 1:
                ticker, ann_date = key
                print(f"  âš  {count} announcements for {ticker} on {ann_date} â€” all kept, later ones flagged for review")
        # Mark the later announcements (all but the first occurrence) for reviewer attention
        seen_first = set()
        for r in results:
            key = (r.get('ticker'), r.get('announcement_date'))
            if key in ticker_day_count and ticker_day_count[key] > 1:
                if key not in seen_first:
                    seen_first.add(key)
                else:
                    r['duplicate_flag'] = True

        # Duplicate tickers: copy rows for tickers that need mirror entries
        # (e.g. VOF transaction also needs an identical VCVOF row)
        duplicate_map = getattr(config, 'DUPLICATE_TICKERS', {})
        duplicated_rows = []
        for r in results:
            ticker = r.get('ticker', '')
            if ticker in duplicate_map:
                for dup_ticker in duplicate_map[ticker]:
                    dup = dict(r)
                    dup['ticker'] = dup_ticker
                    duplicated_rows.append(dup)
                    print(f"  â„¹ Duplicated {ticker} â†’ {dup_ticker}")
        results.extend(duplicated_rows)

        # Phase 3: VALIDATE & OUTPUT
        print("\n" + "=" * 70)
        print("PHASE 3: Validation and Output")
        print("=" * 70)

        # Day-over-day validation
        validations = {}
        if prior_shares:
            print("\nDay-over-day validation:")
            # Add exchange suffix to tickers for matching against prior file
            results_with_ln = []
            for r in results:
                r_copy = dict(r)
                ticker = r_copy.get('ticker', '')
                # Add "LN" suffix unless ticker already has an exchange suffix (e.g. "VCVOF US")
                r_copy['ticker'] = ticker if ' ' in ticker else f"{ticker} LN"
                results_with_ln.append(r_copy)
            validations = Reconciler.validate_all(results_with_ln, prior_shares)

        # Generate output filename
        output_path = os.path.join(script_dir, f'lse_transactions_{timestamp}.xlsx')

        # Write output
        OutputWriter.write(results, output_path, highlights=all_highlights, validations=validations)

        # Print summary report
        print("\n" + "=" * 70)
        print("SUMMARY REPORT")
        print("=" * 70)
        print(f"Total announcements scraped: {len(announcements)}")
        print(f"Successfully extracted: {len(results)}")
        print(f"  Regex OK: {stats['regex_ok']}")
        print(f"  AI reviewer filled gaps: {stats['claude_filled']}")
        print(f"  AI reviewer disagreed: {stats['claude_disagreed']}")
        print(f"  Failed: {stats['failed']}")
        if stats['resolved_tickers'] > 0:
            print(f"  Tickers resolved from text: {stats['resolved_tickers']}")

        if validations:
            valid_count = sum(1 for v in validations.values() if v.get('valid'))
            mismatch_count = sum(1 for v in validations.values() if not v.get('skipped') and not v.get('valid'))
            skipped_count = sum(1 for v in validations.values() if v.get('skipped'))
            print(f"\nDay-over-day validation:")
            print(f"  Passed: {valid_count}")
            print(f"  Mismatches: {mismatch_count}")
            print(f"  Skipped: {skipped_count}")

        if stale_tickers:
            stale_names = [a['ticker'] for a in stale_tickers]
            print(f"\nâš  Content still not available for {len(stale_tickers)} tickers "
                  f"after {config.STALE_RETRY_MAX_ATTEMPTS} retries:")
            print(f"  {', '.join(stale_names)}")

        print(f"\nOutput: {output_path}")
        print("=" * 70)
    finally:
        if log_tee:
            print(f"Log: {log_path}")
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__
            log_tee.close()
            err_tee.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        sys.exit(1)
