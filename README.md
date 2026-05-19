# LSE Buyback Scraper

Daily RNS share-transaction monitoring with a self-improving extraction pipeline.

## Why this exists

The London Stock Exchange's Regulatory News Service publishes share-transaction announcements
from UK Investment Trusts every market day — typically 80-90 per day across buybacks,
issuances, and tender offers. At a global asset manager, the daily output of this scraper
fed ownership-percentage calculations used to decide whether a TR1 disclosure had to be
filed with the FCA inside its 48-hour notification threshold. It replaced roughly two
hours of manual analyst review per day.

## How it works

- **Pull and filter at source.** The LSE News Explorer API is queried with
  `HEADLINE_TYPES = [72, 76]` (Transaction in Own Shares, Issue of Equity) and
  `SECTOR_CODES = [302040, 302020, 302030, 351020]` (Investment Trust categories).
  Only relevant announcements come back; nothing is filtered client-side first.
- **Dual-path extraction with conservative flagging.** Every announcement is parsed in
  parallel by a deterministic regex layer and an AI reviewer. The AI reviewer is the
  source of truth on ambiguous cases; regex is the fast path for boilerplate formats.
  When extraction is uncertain, the row is flagged for human review rather than guessed.
- **Self-improving pattern library.** When regex and AI agree on a ticker for
  `AUTO_LEARN_THRESHOLD = 3` consecutive runs, the pattern the AI implicitly used is
  promoted into the regex library. Any disagreement resets the counter. Over time, the
  system handles more announcements on the fast path without code changes.

## Edge cases handled by name

- Duplicate filings (same ticker, same day) — all rows kept, later ones flagged.
- Multi-announcement days for the same trust.
- Dual share-class structures: **HAN / HANA**, **BHMG / BHMU**, **CMPI / CMPG**.
- Per-ticker currency and voting-rights conversions:
  - **BHMG** — 1.471x voting-rights conversion (GBP class)
  - **BHMU** — 0.7606x voting-rights conversion (USD class)
  - **CTY** — 1 vote per 15 shares
  - **CVCE** — EUR → euro cents
  - **FAIR** — USD pass-through, no conversion to pence
  - **MNTN** — pence → pounds for The Schiehallion Fund

## Reliability

During production use, output was reconciled manually against Bloomberg announcement by
announcement. Straight-through regex extractions were essentially always correct. Items
flagged by the AI reviewer for human attention, on review against Bloomberg, were almost
always already correct — the flagging is conservative by design. No accuracy percentage is
quoted here; the relevant claim is that the system was trusted to feed daily TR1 threshold
calculations and reconciled by hand against Bloomberg in real use.

## AI reviewer — five backends

The reviewer is provider-agnostic. Pick whichever backend matches your environment.

- **Anthropic API** — `ANTHROPIC_API_KEY` required. Default model
  `claude-haiku-4-5-20251001` (override with `ANTHROPIC_MODEL`). Fast, cheap, and
  accurate enough for this structured-extraction task.
- **OpenAI API** — `OPENAI_API_KEY` required. Default model `gpt-4o-mini` (override with
  `OPENAI_MODEL`). Comparable accuracy to Anthropic on this task in informal testing.
- **Claude CLI** — uses a local Claude Code install (`claude` on `PATH`). No API key
  needed if you're already running Claude Code; useful when you want everything to go
  through one workspace.
- **Ollama** — local, fully offline. Tested with `llama3.1:8b`; accuracy on this task is
  noticeably worse than the hosted APIs. Useful when data must not leave the machine,
  not as a quality-first default.
- **`none`** — regex only. Works completely offline, but loses the AI reviewer's
  handling of ambiguous announcements. Reasonable for a quick sanity check.

`--ai-provider auto` (the default) picks the first available provider in this order:
Anthropic API → OpenAI API → Claude CLI → Ollama. If none of the above are available,
the scraper runs in regex-only mode.

## Quick start

```bash
git clone https://github.com/<your-fork>/lse-buyback-scraper.git
cd lse-buyback-scraper
pip install -r requirements.txt

# Optional: set one of the API keys, or skip and use --no-ai / --ai-provider claude_cli
export ANTHROPIC_API_KEY=sk-ant-...

# Offline demo against bundled real-announcement fixtures (no network, ~5 seconds)
python scraper.py --demo --no-ai

# Full live run (requires Chrome/Selenium and an LSE-reachable network)
python scraper.py
```

## Test suite

The repository ships with a pytest suite covering extractor edge cases, ticker-specific
conversions, share-class disambiguation, output formatting, stale-page retry behaviour,
and provider routing. Tests run offline — Selenium, HTTP calls, and the LLM client are
all mocked. To run:

```bash
pytest tests/
```

This is a unit suite, not an end-to-end harness; it exercises code paths against known
fixtures and stubs, not against the live LSE.

## Known limitations

- Tickers whose announcement format hasn't been seen before may produce incomplete or
  AI-flagged output. The optional `not_tracked_tickers.xlsx` lets you skip specific
  tickers entirely (see `not_tracked_tickers.example.txt`).
- The scraper depends on the LSE News Explorer URL structure; if LSE changes it, the
  selectors and API parameters here will need updating.
- Ollama with weaker local models (e.g. `llama3.1:8b`) has lower accuracy than the hosted
  APIs on this task.

## Architecture

- `scraper.py` — orchestration, CLI, multi-phase pipeline (scrape → extract → validate → output).
- `browser.py` — Selenium driver for the LSE SPA; handles the investor-type gate and pagination.
- `lse_api.py` — direct LSE News Explorer API client (preferred over Selenium when available).
- `extractor.py` — regex and ticker-pattern extraction engine.
- `ai_reviewer.py` — provider routing (Anthropic API, OpenAI API, Claude CLI, Ollama, none).
- `claude_reviewer.py` — prompt construction, JSON schema, merge logic, agreement history.
- `reconciler.py` — day-over-day validation against a prior-day output file.
- `output_writer.py` — Excel output with confidence highlighting.
- `config.py` — settings, ticker overrides, share-class maps.
- `ticker_patterns.py` — auto-learned label/value patterns promoted from agreed extractions.
- `logger.py` — `TeeLogger` writing console output to a per-run log file.

## License

MIT — see [LICENSE](LICENSE).

## Author

Dan Krawczun — portfolio at [krawczun.com](https://krawczun.com).
