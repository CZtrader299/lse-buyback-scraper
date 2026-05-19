"""
Browser automation for LSE News Explorer.

This module owns ALL Selenium/browser interaction. No other module touches
the WebDriver directly. Handles Chrome setup, cookie consent, pagination
across listing pages, link extraction, and page text extraction.
"""

import logging
import re
import time

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config

logger = logging.getLogger(__name__)

# XPath selectors for the cookie consent button (tried in order)
_COOKIE_BUTTON_XPATHS = [
    "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept all')]",
    "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept cookies')]",
    "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'agree')]",
    "//button[@id='onetrust-accept-btn-handler']",
    "//button[contains(@class, 'accept') and contains(@class, 'cookie')]",
    "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept')]",
]

# Regex to extract ticker symbol from a news-article URL
_TICKER_RE = re.compile(r"/news-article/([A-Z0-9.]+)/", re.IGNORECASE)


class LSEBrowser:
    """
    Selenium-backed browser for scraping LSE News Explorer.

    Usage::

        with LSEBrowser() as browser:
            links = browser.extract_all_links_with_pagination()
            for link in links:
                text = browser.get_announcement_text(link["url"])
    """

    # ------------------------------------------------------------------ #
    # Construction / teardown
    # ------------------------------------------------------------------ #

    def __init__(self) -> None:
        """Initialise Chrome WebDriver with anti-detection settings."""
        options = Options()

        if config.HEADLESS:
            options.add_argument("--headless=new")

        # Anti-detection flags
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        # Realistic user-agent
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )

        # Stability / performance options
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

        service = Service()
        self._driver = webdriver.Chrome(service=service, options=options)
        self._driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)

        # CDP override: hide webdriver property from JavaScript
        self._driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
            },
        )

        logger.info("Chrome WebDriver initialised (headless=%s)", config.HEADLESS)

    def close(self) -> None:
        """Quit the browser and release resources."""
        try:
            self._driver.quit()
            logger.info("Chrome WebDriver closed.")
        except WebDriverException as exc:
            logger.warning("Error closing WebDriver: %s", exc)

    def __enter__(self) -> "LSEBrowser":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Cookie consent
    # ------------------------------------------------------------------ #

    def accept_cookies(self) -> bool:
        """
        Attempt to dismiss the cookie consent banner.

        Tries each of the six XPath selectors defined in
        ``_COOKIE_BUTTON_XPATHS`` in turn.  Returns ``True`` if a button
        was found and clicked, ``False`` otherwise.
        """
        wait = WebDriverWait(self._driver, 8)
        for xpath in _COOKIE_BUTTON_XPATHS:
            try:
                button = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                button.click()
                logger.info("Cookie consent accepted (xpath: %s)", xpath)
                time.sleep(1)
                return True
            except TimeoutException:
                continue
            except (ElementClickInterceptedException, WebDriverException) as exc:
                logger.debug("Cookie button click failed (%s): %s", xpath, exc)
                continue

        logger.debug("No cookie consent banner found or already dismissed.")
        return False

    # ------------------------------------------------------------------ #
    # Page navigation
    # ------------------------------------------------------------------ #

    def load_news_page(self) -> None:
        """
        Navigate to the LSE News Explorer URL built from config settings.

        Waits for the page body to be present, then attempts to dismiss
        any cookie consent banner.
        """
        url = config.build_news_url()
        logger.info("Loading news page: %s", url)
        self._driver.get(url)

        try:
            WebDriverWait(self._driver, config.PAGE_LOAD_TIMEOUT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            logger.warning("Timed out waiting for page body on %s", url)

        time.sleep(5)  # allow JS SPA to load and apply filters
        self.accept_cookies()

    # ------------------------------------------------------------------ #
    # Link extraction — single page
    # ------------------------------------------------------------------ #

    def extract_links_from_current_page(
        self, seen_article_ids: set
    ) -> list[dict]:
        """
        Extract announcement links from the currently loaded listing page.

        Scrolls through the page to trigger any lazy-loaded content, then
        collects all anchor tags whose ``href`` contains ``/news-article/``.
        Duplicate detection is based on the ``article_id`` component of
        the URL.

        Parameters
        ----------
        seen_article_ids:
            Set of article IDs already collected in previous pages.
            Updated in-place with IDs found on this page.

        Returns
        -------
        list of dict, each with keys:
            ``ticker``    — uppercase ticker symbol (e.g. ``"CTY"``)
            ``title``     — link text (stripped)
            ``url``       — absolute URL to the announcement
            ``article_id``— unique identifier extracted from the URL path
        """
        self._scroll_page()

        anchors = self._driver.find_elements(By.TAG_NAME, "a")
        new_links: list[dict] = []

        for anchor in anchors:
            try:
                href = anchor.get_attribute("href") or ""
            except StaleElementReferenceException:
                continue

            if "/news-article/" not in href:
                continue

            # Derive article_id: last non-empty path segment
            path_parts = [p for p in href.rstrip("/").split("/") if p]
            article_id = path_parts[-1] if path_parts else ""

            if not article_id or article_id in seen_article_ids:
                continue

            # Extract ticker via regex — None if URL uses non-standard path
            match = _TICKER_RE.search(href)
            if match:
                ticker = match.group(1).upper()
            else:
                ticker = None
                logger.info("Non-standard URL (no ticker in path): %s", href)

            try:
                title = anchor.text.strip()
            except StaleElementReferenceException:
                title = ""

            seen_article_ids.add(article_id)
            new_links.append(
                {
                    "ticker": ticker,
                    "title": title,
                    "url": href,
                    "article_id": article_id,
                }
            )

        logger.debug("Found %d new links on current page.", len(new_links))
        return new_links

    # ------------------------------------------------------------------ #
    # Link extraction — all pages (pagination)
    # ------------------------------------------------------------------ #

    def extract_all_links_with_pagination(self) -> list[dict]:
        """
        Collect announcement links across all paginated listing pages.

        Iterates up to ``config.MAX_PAGES`` pages, extracting links from
        each.  Stops early when no next-page button is available or when
        no new links are found on a page (indicating the end of results).

        A health-check warning is emitted when the total collected is fewer
        than ``config.MIN_EXPECTED_ANNOUNCEMENTS``.

        Returns
        -------
        list of dicts (same structure as
        :meth:`extract_links_from_current_page`), deduplicated across all
        pages.
        """
        all_links: list[dict] = []
        seen_article_ids: set = set()

        self.load_news_page()

        for page_num in range(1, config.MAX_PAGES + 1):
            logger.info("Scraping page %d of up to %d...", page_num, config.MAX_PAGES)
            page_links = self.extract_links_from_current_page(seen_article_ids)
            all_links.extend(page_links)

            if not page_links:
                logger.info(
                    "No new links found on page %d — assuming end of results.",
                    page_num,
                )
                break

            if page_num == config.MAX_PAGES:
                logger.warning(
                    "Reached MAX_PAGES (%d) — there may be more results.",
                    config.MAX_PAGES,
                )
                break

            advanced = self._click_next_page(page_num)
            if not advanced:
                logger.info("No next-page button found after page %d.", page_num)
                break

            time.sleep(config.REQUEST_DELAY)

        total = len(all_links)
        if total < config.MIN_EXPECTED_ANNOUNCEMENTS:
            logger.warning(
                "Health check: only %d announcements collected "
                "(expected >= %d). Check the site or filters.",
                total,
                config.MIN_EXPECTED_ANNOUNCEMENTS,
            )
        else:
            logger.info("Collected %d announcement links in total.", total)

        return all_links

    # ------------------------------------------------------------------ #
    # Pagination helper
    # ------------------------------------------------------------------ #

    def _click_next_page(self, current_page: int) -> bool:
        """
        Advance to the next listing page.

        Tries the following strategies in order:

        1. A button/link whose visible text is the next page number.
        2. A generic "Next" / "Next page" button.
        3. JavaScript click fallback if a normal click is intercepted.

        Parameters
        ----------
        current_page:
            The 1-based index of the page that is currently displayed.

        Returns
        -------
        ``True`` if navigation was successfully triggered, ``False`` if no
        suitable element was found.
        """
        next_page_num = current_page + 1
        next_page_str = str(next_page_num)

        # Strategy 1: numbered page buttons
        numbered_xpaths = [
            f"//button[normalize-space(text())='{next_page_str}']",
            f"//a[normalize-space(text())='{next_page_str}' and contains(@class,'page')]",
            f"//li[contains(@class,'page')]//a[normalize-space(text())='{next_page_str}']",
        ]

        # Strategy 2: "Next" labels
        next_label_xpaths = [
            "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
            "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
            "//li[contains(@class,'next')]//a",
            "//*[@aria-label and contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next page')]",
        ]

        all_xpaths = numbered_xpaths + next_label_xpaths

        for xpath in all_xpaths:
            try:
                element = WebDriverWait(self._driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                if not element.is_displayed() or not element.is_enabled():
                    continue
                try:
                    element.click()
                except ElementClickInterceptedException:
                    # JS click fallback
                    self._driver.execute_script("arguments[0].click();", element)

                logger.debug("Clicked next-page element (xpath: %s)", xpath)
                time.sleep(4)  # allow SPA to load new page content
                return True

            except (TimeoutException, NoSuchElementException):
                continue
            except (StaleElementReferenceException, WebDriverException) as exc:
                logger.debug("Next-page click failed (%s): %s", xpath, exc)
                continue

        return False

    # ------------------------------------------------------------------ #
    # Individual announcement text
    # ------------------------------------------------------------------ #

    def get_announcement_text(self, url: str) -> str:
        """
        Navigate to an individual announcement URL and return its text.

        Tries four extraction strategies in order, returning the first
        result that is longer than 100 characters:

        1. Element with CSS class ``article-content``
        2. ``<main>`` tag
        3. ``<article>`` tag
        4. ``<body>`` tag

        Parameters
        ----------
        url:
            Absolute URL to the announcement page.

        Returns
        -------
        Extracted text (stripped), or an empty string if no suitable
        content was found.
        """
        logger.debug("Fetching announcement text: %s", url)

        try:
            self._driver.get(url)
            WebDriverWait(self._driver, config.PAGE_LOAD_TIMEOUT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            logger.warning("Page load timed out: %s", url)
            return ""
        except WebDriverException as exc:
            logger.error("Failed to load announcement page %s: %s", url, exc)
            return ""

        time.sleep(3)  # allow SPA JS content to fully render (matches original scraper)

        extraction_strategies = [
            (By.CLASS_NAME, "article-content"),
            (By.TAG_NAME, "main"),
            (By.TAG_NAME, "article"),
            (By.TAG_NAME, "body"),
        ]

        for by, value in extraction_strategies:
            try:
                element = self._driver.find_element(by, value)
                text = element.text.strip()
                if len(text) > 100:
                    logger.debug(
                        "Extracted %d chars using (%s, %s) from %s",
                        len(text),
                        by,
                        value,
                        url,
                    )
                    return text
            except NoSuchElementException:
                continue
            except WebDriverException as exc:
                logger.debug(
                    "Extraction strategy (%s, %s) failed: %s", by, value, exc
                )
                continue

        logger.warning("Could not extract usable text (>100 chars) from %s", url)
        return ""

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _scroll_page(self) -> None:
        """
        Scroll gradually through the current page to trigger lazy loading.

        Performs three scroll steps (33 %, 66 %, 100 % of page height)
        with a short pause between each, then returns to the top.
        """
        try:
            scroll_height = self._driver.execute_script(
                "return document.body.scrollHeight"
            )
            for fraction in (0.33, 0.66, 1.0):
                self._driver.execute_script(
                    "window.scrollTo(0, arguments[0]);",
                    int(scroll_height * fraction),
                )
                time.sleep(0.5)
            # Return to top so pagination buttons are accessible
            self._driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.3)
        except WebDriverException as exc:
            logger.debug("Scroll failed (non-fatal): %s", exc)
