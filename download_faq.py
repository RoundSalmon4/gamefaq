#!/usr/bin/env python3
"""
Download a GameFAQs.com FAQ in plain text format.

Uses Firecrawl and ScrapingBee to bypass Cloudflare protections.

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
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse, urlunparse

import html2text
import requests as http_requests
from playwright.sync_api import sync_playwright
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
        r"Performing security verification",
        r"security service to protect against",
    )
]


class FetchResult(NamedTuple):
    content: str
    is_html: bool


class FAQDownloadError(Exception):
    pass


def _ensure_single_param(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if params.get("single") == ["1"]:
        return url
    params["single"] = ["1"]
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


def _resolve_game_url(url: str) -> str:
    """Given a game page URL, find the best FAQ URL and return it.
    Uses Brave Search to avoid direct access blocks from datacenter IPs.
    Returns the original URL if it's already a FAQ URL."""
    base_url = url.split("?")[0]
    if RE_FAQ_URL.match(base_url):
        return url

    logger.info("Game page URL detected — searching Brave for FAQs")

    slug = ""
    m = re.search(r"gamefaqs\.gamespot\.com/[a-z0-9-]+/(\d+-[^/?]+)", base_url)
    if m:
        slug = m.group(1)

    game_title = re.sub(r"^\d+-", "", slug).replace("-", " ").title() if slug else ""
    if not game_title:
        raise FAQDownloadError(
            f"Could not extract game title from URL: {url}"
        )

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

            query = f"site:gamefaqs.gamespot.com {game_title}"
            brave_url = f"https://search.brave.com/search?q={quote_plus(query)}"
            logger.info("Searching Brave: %s", query)
            page.goto(brave_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            try:
                page.wait_for_selector(
                    "a[href*='gamefaqs.gamespot.com']",
                    timeout=15_000,
                )
            except PlaywrightTimeoutError:
                pass
            time.sleep(1)

            all_links = page.locator("a[href*='gamefaqs.gamespot.com']").all()
            best_url = None
            best_rank = 999

            rating_order = {
                "highest rated": 1,
                "most recommended": 2,
                "complete": 3,
                "partial": 4,
            }

            seen: set[str] = set()
            for link in all_links:
                try:
                    href = link.get_attribute("href") or ""
                    if not href or href in seen:
                        continue
                    if not re.search(r"/faqs/\d+", href):
                        continue
                    if not href.startswith("http"):
                        href = f"https://gamefaqs.gamespot.com{href}"
                    # Strip chapter slugs — keep only /faqs/<id>
                    href = re.sub(r"(/faqs/\d+).*", r"\1", href)
                    if href in seen:
                        continue
                    seen.add(href)

                    rank = 5
                    try:
                        parent = link.locator(
                            "xpath=ancestor::div[contains(@class,'snippet') or contains(@class,'result')]"
                        ).first
                        if parent.count() > 0:
                            snippet = parent.inner_text().strip().lower()
                            for kw, r in rating_order.items():
                                if kw in snippet:
                                    rank = r
                                    break
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
                f"Could not find any FAQ guides for '{game_title}'. "
                f"Try providing a direct FAQ URL instead."
            )
        finally:
            browser.close()


class FAQDownloader:
    def __init__(self, url: str, output_dir: str = ".",
                 scrapingbee_key: str | None = None,
                 firecrawl_key: str | None = None) -> None:
        base_url = url.split("?")[0]
        if not _validate_url(url):
            raise FAQDownloadError(
                f"Invalid URL — expected a GameFAQs FAQ or game page URL. Got: {url}"
            )
        self.url = _resolve_game_url(url)
        self.url = _ensure_single_param(self.url)
        self.output_dir = os.path.expanduser(output_dir)
        self.scrapingbee_key = scrapingbee_key
        self.firecrawl_key = firecrawl_key

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
                if self.firecrawl_key:
                    return self._fetch_content_firecrawl()
                if self.scrapingbee_key:
                    return self._fetch_content_scrapingbee()
                raise FAQDownloadError(
                    "No API key provided — set FIRECRAWL_API_KEY or SCRAPINGBEE_API_KEY."
                )
            except FAQDownloadError as exc:
                last_err = exc
                if self.firecrawl_key:
                    logger.warning("Firecrawl failed, trying ScrapingBee...")
                    self.firecrawl_key = None
                    continue
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

    def _fetch_content_firecrawl(self) -> FetchResult:
        """Fetch via Firecrawl API — uses managed infrastructure to bypass
        Cloudflare and IP-level blocks."""
        api_url = "https://api.firecrawl.dev/v2/scrape"
        headers = {
            "Authorization": f"Bearer {self.firecrawl_key}",
            "Content-Type": "application/json",
        }
        logger.info("Trying Firecrawl for %s", self.url)

        payload = {
            "url": self.url,
            "formats": ["markdown"],
            "onlyMainContent": True,
            "removeBase64Images": True,
        }
        try:
            resp = http_requests.post(
                api_url, headers=headers, json=payload, timeout=120
            )
        except http_requests.RequestException as exc:
            raise FAQDownloadError(f"Firecrawl request failed: {exc}") from exc

        if resp.status_code == 402:
            raise FAQDownloadError(
                "Firecrawl credits exhausted — check your plan."
            )
        if resp.status_code == 429:
            raise FAQDownloadError(
                "Firecrawl rate limited — retry later."
            )
        resp.raise_for_status()

        data = resp.json()
        logger.debug("Firecrawl response keys: %s", list(data.keys()))
        if not data.get("success"):
            msg = data.get("error", "unknown error")
            raise FAQDownloadError(f"Firecrawl error: {msg}")

        inner = data.get("data", {})
        logger.debug("Firecrawl inner data keys: %s", list(inner.keys()))
        markdown = inner.get("markdown", "")
        if not markdown or len(markdown.strip()) < MIN_CONTENT_LENGTH:
            raise FAQDownloadError("Firecrawl returned empty or too-short content.")

        blocked = "performing security verification" in markdown.lower() or (
            "request blocked" in markdown.lower()
            and "abuse from this hosting" in markdown.lower()
        )
        if blocked:
            raise FAQDownloadError(
                "Firecrawl could not bypass Cloudflare for this URL."
            )
        logger.info("Firecrawl fetch succeeded (%d chars)", len(markdown))
        return FetchResult(content=markdown, is_html=False)

    def _fetch_content_scrapingbee(self) -> FetchResult:
        """Fetch via ScrapingBee API — uses residential IPs to bypass
        Cloudflare and IP-level blocks."""
        api_url = "https://app.scrapingbee.com/api/v1/"
        logger.info("Trying ScrapingBee for %s", self.url)

        params = {
            "api_key": self.scrapingbee_key,
            "url": self.url,
            "render_js": "true",
            "premium_proxy": "true",
            "stealth_proxy": "true",
            "return_page_source": "true",
        }
        resp = http_requests.get(api_url, params=params, timeout=120)
        if resp.status_code == 402:
            raise FAQDownloadError(
                "ScrapingBee credits exhausted — check your plan."
            )
        if resp.status_code == 429:
            raise FAQDownloadError(
                "ScrapingBee rate limited — retry later."
            )
        resp.raise_for_status()
        html = resp.text
        if len(html) < MIN_CONTENT_LENGTH:
            raise FAQDownloadError("ScrapingBee returned empty or too-short content.")
        blocked = "performing security verification" in html.lower() or (
            "request blocked" in html.lower()
            and "abuse from this hosting" in html.lower()
        )
        if blocked:
            raise FAQDownloadError(
                "ScrapingBee could not bypass Cloudflare for this URL."
            )
        logger.info("ScrapingBee fetch succeeded (%d bytes)", len(html))
        return FetchResult(content=html, is_html=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download GameFAQs guides as text files.",
    )
    parser.add_argument("url", help="The URL of the FAQ to download")
    parser.add_argument(
        "-o", "--output", default="guides",
        help="Output directory (default: guides/)",
    )
    parser.add_argument(
        "-s", "--scrapingbee", default=None, metavar="KEY",
        help="ScrapingBee API key to bypass Cloudflare via residential IPs",
    )
    parser.add_argument(
        "--firecrawl", default=None, metavar="KEY",
        help="Firecrawl API key to bypass Cloudflare (primary method)",
    )
    args = parser.parse_args()
    try:
        downloader = FAQDownloader(args.url, args.output,
                                   scrapingbee_key=args.scrapingbee,
                                   firecrawl_key=args.firecrawl)
        filepath = downloader.fetch_and_save()
        print(filepath)
    except FAQDownloadError as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
