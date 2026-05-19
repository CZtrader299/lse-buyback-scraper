"""Provider-selectable AI reviewer for edge-case extraction.

The scraper's merge, prompt, and history behavior still lives in
``ClaudeReviewer``. This wrapper changes only the runtime provider so the
scraper is not hard-wired to Claude CLI.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

import requests

import config
from claude_reviewer import (
    CLAUDE_MODEL,
    EXTRACTION_SCHEMA,
    MULTI_CLASS_SCHEMA,
    ClaudeReviewer,
)


class AIReviewer(ClaudeReviewer):
    """Run review prompts through a configured provider.

    Providers:
    - none: disable AI review
    - auto: prefer hosted APIs (Anthropic > OpenAI), fall back to Ollama, then Claude CLI
    - anthropic_api: call Anthropic's Messages API directly (requires ANTHROPIC_API_KEY)
    - openai_api: call OpenAI's Chat Completions API (requires OPENAI_API_KEY)
    - ollama: use a locally running Ollama server
    - claude_cli: use the legacy ``claude`` command
    """

    PROVIDERS = {"none", "auto", "ollama", "claude_cli", "anthropic_api", "openai_api"}

    def __init__(self, provider=None):
        self._history_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ticker_review_history.json"
        )
        requested = (provider or config.AI_REVIEW_PROVIDER or "none").strip().lower()
        if requested == "claude":
            requested = "claude_cli"
        if requested not in self.PROVIDERS:
            print(f"  Warning: unknown AI provider '{requested}', disabling AI review")
            requested = "none"

        self.provider = self._resolve_provider(requested)
        self.available = self.provider != "none"

        if self.available:
            print(f"  AI reviewer enabled: {self.provider}")
        else:
            if requested == "none":
                print("  AI reviewer disabled (regex-only mode)")
            else:
                print(f"  AI provider '{requested}' unavailable - regex-only mode")

    def _resolve_provider(self, requested):
        if requested == "none":
            return "none"
        if requested == "anthropic_api":
            return "anthropic_api" if self._check_anthropic_api_available() else "none"
        if requested == "openai_api":
            return "openai_api" if self._check_openai_api_available() else "none"
        if requested == "ollama":
            return "ollama" if self._check_ollama_available() else "none"
        if requested == "claude_cli":
            return "claude_cli" if self._check_cli_available() else "none"
        if requested == "auto":
            # Priority: hosted APIs first (fastest, cheapest, highest quality),
            # then Claude CLI (uses a real Claude subscription), then Ollama
            # (local small models — works offline but lower accuracy in testing).
            # Ollama is last because it's often left running as a background
            # service on dev machines even when the user prefers Claude.
            if self._check_anthropic_api_available():
                return "anthropic_api"
            if self._check_openai_api_available():
                return "openai_api"
            if self._check_cli_available():
                return "claude_cli"
            if self._check_ollama_available():
                return "ollama"
        return "none"

    @staticmethod
    def _check_anthropic_api_available():
        # No network probe; just check the key is set. The first call surfaces auth errors.
        return bool(os.environ.get("ANTHROPIC_API_KEY") or getattr(config, "ANTHROPIC_API_KEY", ""))

    @staticmethod
    def _check_openai_api_available():
        return bool(os.environ.get("OPENAI_API_KEY") or getattr(config, "OPENAI_API_KEY", ""))

    @staticmethod
    def _check_ollama_available():
        try:
            resp = requests.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=3)
            return resp.ok
        except requests.RequestException:
            return False

    def review_multi_class(self, page_text, ticker, class_tickers):
        if not self.available:
            return None

        prompt = self.build_multi_class_prompt(page_text, ticker, class_tickers)
        parsed = self._run_json_prompt(prompt, MULTI_CLASS_SCHEMA, "multi-class")
        if parsed and isinstance(parsed.get("share_classes"), list):
            classes = parsed["share_classes"]
            print(f"    OK AI reviewer identified {len(classes)} share class(es)")
            return classes
        return None

    def review(self, page_text, ticker):
        if not self.available:
            return None

        prompt = self.build_prompt(page_text, ticker)
        return self._run_json_prompt(prompt, EXTRACTION_SCHEMA, "extraction")

    def identify_ticker(self, page_text):
        if not self.available:
            return None

        prompt = self.build_identify_ticker_prompt(page_text)
        parsed = self._run_json_prompt(prompt, self._TICKER_ID_SCHEMA, "ticker ID")
        if parsed and parsed.get("ticker"):
            ticker = str(parsed["ticker"]).upper().strip()
            confidence = parsed.get("confidence", "unknown")
            print(f"    OK AI reviewer identified ticker: {ticker} (confidence: {confidence})")
            return ticker
        return None

    def _run_json_prompt(self, prompt, schema, label):
        for attempt in range(1 + config.CLAUDE_MAX_RETRIES):
            try:
                if self.provider == "claude_cli":
                    parsed = self._run_claude_cli(prompt, schema)
                elif self.provider == "ollama":
                    parsed = self._run_ollama(prompt)
                elif self.provider == "anthropic_api":
                    parsed = self._run_anthropic_api(prompt, schema)
                elif self.provider == "openai_api":
                    parsed = self._run_openai_api(prompt, schema)
                else:
                    return None
            except TimeoutError:
                print(f"    Warning: AI reviewer timed out for {label} (attempt {attempt + 1})")
                continue
            except Exception as exc:
                print(f"    Warning: AI reviewer error for {label}: {exc}")
                continue

            if parsed:
                return parsed
            print(f"    Warning: could not parse AI reviewer response for {label} (attempt {attempt + 1})")

        return None

    def _run_claude_cli(self, prompt, schema):
        # On Windows, claude ships as a .cmd shim that bare subprocess doesn't
        # resolve. shutil.which() honors PATHEXT and finds claude.cmd / claude.exe
        # correctly on all platforms. Resolving once here means the rest of the
        # subprocess call can stay shell=False and quote-safe even with the JSON
        # schema arg, which would be a nightmare to escape under shell=True.
        claude_path = shutil.which("claude") or "claude"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            temp_path = f.name

        try:
            with open(temp_path, "r", encoding="utf-8") as prompt_file:
                result = subprocess.run(
                    [
                        claude_path,
                        "-p",
                        "-",
                        "--output-format",
                        "json",
                        "--model",
                        CLAUDE_MODEL,
                        "--tools",
                        "",
                        "--no-session-persistence",
                        "--json-schema",
                        json.dumps(schema),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=config.CLAUDE_TIMEOUT,
                    stdin=prompt_file,
                    env=self._clean_env(),
                )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError from exc
        finally:
            os.unlink(temp_path)

        if result.returncode != 0:
            print(f"    Warning: Claude CLI exited with code {result.returncode}")
            return None
        return self.parse_response(result.stdout)

    def _run_ollama(self, prompt):
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.OLLAMA_MODEL,
                "prompt": self._json_only_prompt(prompt),
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            },
            timeout=config.OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        return self._parse_plain_json(payload.get("response", ""))

    def _run_anthropic_api(self, prompt, schema):
        api_key = os.environ.get("ANTHROPIC_API_KEY") or getattr(config, "ANTHROPIC_API_KEY", "")
        model = os.environ.get("ANTHROPIC_MODEL") or getattr(
            config, "ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
        )
        body = {
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": self._json_only_prompt(prompt)}],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                json=body,
                headers=headers,
                timeout=config.CLAUDE_TIMEOUT,
            )
        except requests.Timeout as exc:
            raise TimeoutError from exc
        if not response.ok:
            print(f"    Warning: Anthropic API HTTP {response.status_code}: {response.text[:200]}")
            return None
        payload = response.json()
        try:
            text = payload["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            return None
        return self._parse_plain_json(text)

    def _run_openai_api(self, prompt, schema):
        api_key = os.environ.get("OPENAI_API_KEY") or getattr(config, "OPENAI_API_KEY", "")
        model = os.environ.get("OPENAI_MODEL") or getattr(config, "OPENAI_MODEL", "gpt-4o-mini")
        body = {
            "model": model,
            "messages": [{"role": "user", "content": self._json_only_prompt(prompt)}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                json=body,
                headers=headers,
                timeout=config.CLAUDE_TIMEOUT,
            )
        except requests.Timeout as exc:
            raise TimeoutError from exc
        if not response.ok:
            print(f"    Warning: OpenAI API HTTP {response.status_code}: {response.text[:200]}")
            return None
        payload = response.json()
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        return self._parse_plain_json(text)

    @staticmethod
    def _json_only_prompt(prompt):
        return (
            f"{prompt}\n\n"
            "Return only a valid JSON object. Do not include markdown, comments, "
            "or explanatory text."
        )

    @staticmethod
    def _parse_plain_json(text):
        if not text:
            return None

        if isinstance(text, dict):
            return text

        content = str(text).strip()
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if json_match:
            content = json_match.group(1).strip()

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
