#!/usr/bin/env python3
"""
Download a GameFAQs.com FAQ in plain text format.

Uses Playwright to bypass Cloudflare protections.
Adapted from https://gist.github.com/alechemy/ed84c5b6b53b5194f1875b96a9a4faf1

Usage:
    python download_faq.py <url> [-o output_dir]

Accepts either a direct FAQ URL or a game page URL (auto-finds top guide).

Examples:
    python download_faq.py https://gamefaqs.gamespot.com/ps/196853-final-fantasy-vii/faqs/57145
    python download_faq.py https://gamefaqs.gamespot.com/ps/196853-final-fantasy-vii
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from typing import NamedTuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import html2text
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

RE_FAQ_URL = re.compile(
    r"^https?://(www\.)?gamefaqs\.gamespot\.com/.+/faqs/[0-9]{3,8}/?$",
    re.IGNORECASE,
)

RE_GAME_URL = re.compile(
    r"^https?://(www\.)?gamefaqs\.gamespot\.com/[a-z0-9-]+/\d+-[^/?]+/?$",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

NAV_TIMEOUT_MS = 60_000
SELECTOR_TIMEOUT_MS = 15_000
CLOUDFLARE_POLL_INTERVAL = 0.5
CLOUDFLARE_MAX_WAIT = 15
MAX_RETRIES = 2
MIN_CONTENT_LENGTH = 100

_JUNK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"Verify you are human",
        r"needs to review the security of your connection",
        r"Enable JavaScript and cookies to continue",
        r"Ray ID:",
        r"Performance & security by",
        r"Waiting for gamefaqs",
        r"Verification successful",
        r"\bCloudflare\b",
        r"This website uses cookies",
        r"We also share information about your use of our site",
        r"\[Privacy Policy\]",
        r"^\s*Just a moment\s*$",
    )
]

_CF_CHALLENGE_INDICATORS = (
    "text='Verify you are human'",
    "text='Just a moment'",
    "#challenge-running",
    "#cf-challenge-running",
)

_CONTENT_SELECTORS = '[id^="faqspan-"], .faqtext, #content, pre'


class FetchResult(NamedTuple):
    content: str
    is_html: bool


class FAQDownloadError(Exception):
    pass


def _ensure_print_param(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if params.get("print") == ["1"]:
        return url
    params["print"] = ["1"]
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _validate_url(url: str) -> bool:
    base_url = url.split("?")[0]
    return bool(RE_FAQ_URL.match(base_url)) or bool(RE_GAME_URL.match(base_url))


def _generate_filename(url: str) -> str:
    try:
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) >= 4:
            console = parts[0]
            game = parts[1]
            faq_id = parts[-1]
            return f"{console}-{game}-faq-{faq_id}.txt"
        elif len(parts) >= 2:
            return f"{parts[1]}.txt"
    except Exception:
        pass
    return "gamefaqs_download.txt"


def _clean_content(text: str) -> str:
    cleaned: list[str] = []
    prev_was_junk = False
    for line in text.split("\n"):
        if any(pat.search(line) for pat in _JUNK_PATTERNS):
            prev_was_junk = True
            continue
        if prev_was_junk and not line.strip():
            continue
        prev_was_junk = False
        cleaned.append(line)
    return "\n".join(cleaned).rstrip()


def _has_cloudflare_challenge(page: Page) -> bool:
    for selector in _CF_CHALLENGE_INDICATORS:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


def _wait_for_cloudflare(page: Page) -> None:
    if not _has_cloudflare_challenge(page):
        return
    logger.info("Cloudflare challenge detected - waiting for it to clear...")
    deadline = time.monotonic() + CLOUDFLARE_MAX_WAIT
    while time.monotonic() < deadline:
        page.wait_for_timeout(int(CLOUDFLARE_POLL_INTERVAL * 1000))
        if not _has_cloudflare_challenge(page):
            logger.info("Cloudflare challenge cleared.")
            return
    logger.warning(
        "Cloudflare challenge did not clear within %d s - proceeding anyway.",
        CLOUDFLARE_MAX_WAIT,
    )


def _extract_content(page: Page) -> FetchResult | None:
    spans = page.locator('[id^="faqspan-"]')
    span_count = spans.count()
    if span_count > 0:
        logger.info("Found %d faqspan element(s) - extracting.", span_count)
        parts: list[str] = []
        for i in range(span_count):
            parts.append(spans.nth(i).inner_text())
        return FetchResult(content="\n".join(parts), is_html=False)

    loc = page.locator(".faqtext").first
    if loc.count() > 0:
        is_pre: bool = loc.evaluate("el => el.tagName === 'PRE'")
        if is_pre:
            return FetchResult(content=loc.inner_text(), is_html=False)
        return FetchResult(content=loc.outer_html(), is_html=True)

    pres = page.locator("pre").all()
    if pres:
        best = max(pres, key=lambda el: len(el.inner_text()))
        text = best.inner_text()
        if len(text.strip()) >= MIN_CONTENT_LENGTH:
            return FetchResult(content=text, is_html=False)

    full = page.content()
    if len(full.strip()) >= MIN_CONTENT_LENGTH:
        logger.info("Using full-page HTML as fallback.")
        return FetchResult(content=full, is_html=True)

    return None


def _resolve_game_url(url: str) -> str:
    """Given a game page URL, navigate to its FAQs listing and return the
    top-rated FAQ URL. Returns the original URL if it's already a FAQ URL."""
    base_url = url.split("?")[0]
    if RE_FAQ_URL.match(base_url):
        return url

    faq_listing = base_url.rstrip("/") + "/faqs/"
    logger.info("Game page URL detected — fetching FAQ listing from %s", faq_listing)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
            )
            context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver',
                    { get: () => undefined });
                Object.defineProperty(navigator, 'plugins',
                    { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages',
                    { get: () => ['en-US', 'en'] });
                window.chrome = { runtime: {} };
                """
            )
            page = context.new_page()
            page.goto(faq_listing, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            _wait_for_cloudflare(page)
            time.sleep(2)

            page_text = page.inner_text("body")
            if "Request Blocked" in page_text or "abuse from this hosting" in page_text:
                raise FAQDownloadError("GameFAQs blocked direct access to the FAQ listing page")

            faq_links = page.locator("a[href*='/faqs/']").all()
            best_url = None
            best_rank = 999

            rating_order = {
                "Highest Rated": 1,
                "Most Recommended": 2,
                "Complete": 3,
                "Partial": 4,
            }

            for link in faq_links:
                try:
                    href = link.get_attribute("href") or ""
                    if not re.search(r"/faqs/\d+", href):
                        continue
                    if not href.startswith("http"):
                        href = f"https://gamefaqs.gamespot.com{href}"

                    rank = 5
                    try:
                        parent = link.locator("xpath=ancestor::tr").first
                        if parent.count() > 0:
                            icon = parent.locator("i[title]").first
                            if icon.count() > 0:
                                title_attr = icon.get_attribute("title") or ""
                                rank = rating_order.get(title_attr, 5)
                    except Exception:
                        pass

                    if rank < best_rank:
                        best_rank = rank
                        best_url = href
                except Exception:
                    continue

            if best_url:
                logger.info("Auto-selected FAQ: %s (rank %d)", best_url, best_rank)
                return best_url

            raise FAQDownloadError(
                f"Could not find any FAQ guides on the game page. "
                f"Try providing a direct FAQ URL instead."
            )
        finally:
            browser.close()


class FAQDownloader:
    def __init__(self, url: str, output_dir: str = ".") -> None:
        base_url = url.split("?")[0]
        if not _validate_url(url):
            raise FAQDownloadError(
                f"Invalid URL — expected a GameFAQs FAQ or game page URL. Got: {url}"
            )
        self.url = _resolve_game_url(url)
        self.url = _ensure_print_param(self.url)
        self.output_dir = os.path.expanduser(output_dir)

    def fetch_and_save(self) -> str:
        result = self._fetch_with_retries()
        if result.is_html:
            h = html2text.HTML2Text()
            h.body_width = 0
            text = h.handle(result.content)
        else:
            text = result.content
        text = _clean_content(text)
        filename = _generate_filename(self.url)
        filepath = os.path.join(self.output_dir, filename)
        os.makedirs(self.output_dir, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(text)
        logger.info('Saved to "%s"', filepath)
        return filepath

    def _fetch_with_retries(self) -> FetchResult:
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self._fetch_content_playwright()
            except FAQDownloadError as exc:
                last_err = exc
                if attempt < MAX_RETRIES:
                    wait = 2 ** attempt
                    logger.warning(
                        "Attempt %d/%d failed. Retrying in %d s...",
                        attempt, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
        raise FAQDownloadError(
            f"All {MAX_RETRIES} attempt(s) failed."
        ) from last_err

    def _fetch_content_playwright(self) -> FetchResult:
        logger.info("Navigating to %s ...", self.url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                )
                context.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver',
                        { get: () => undefined });
                    Object.defineProperty(navigator, 'plugins',
                        { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'languages',
                        { get: () => ['en-US', 'en'] });
                    window.chrome = { runtime: {} };
                    """
                )
                page = context.new_page()
                try:
                    page.goto(
                        self.url,
                        wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT_MS,
                    )
                except PlaywrightTimeoutError as exc:
                    raise FAQDownloadError(
                        f"Navigation timed out after {NAV_TIMEOUT_MS} ms"
                    ) from exc

                _wait_for_cloudflare(page)

                try:
                    page.wait_for_selector(
                        _CONTENT_SELECTORS, timeout=SELECTOR_TIMEOUT_MS
                    )
                except PlaywrightTimeoutError:
                    logger.warning(
                        "Timed out waiting for content selectors - "
                        "will attempt extraction anyway."
                    )

                result = _extract_content(page)
                if result is None or len(result.content.strip()) < MIN_CONTENT_LENGTH:
                    raise FAQDownloadError(
                        "Extracted content was empty or too short."
                    )
                return result
            except FAQDownloadError:
                raise
            except Exception as exc:
                raise FAQDownloadError(f"Playwright error: {exc}") from exc
            finally:
                browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download GameFAQs guides as text files.",
    )
    parser.add_argument("url", help="The URL of the FAQ to download")
    parser.add_argument(
        "-o", "--output", default="guides",
        help="Output directory (default: guides/)",
    )
    args = parser.parse_args()
    try:
        downloader = FAQDownloader(args.url, args.output)
        filepath = downloader.fetch_and_save()
        print(filepath)
    except FAQDownloadError as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
