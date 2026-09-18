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
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import html2text
import requests as http_requests

from search_faq import FAQGuide, _extract_faq_links, _firecrawl_scrape

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
            return f"{console}-{game}-faq-{faq_id}.md"
        elif len(parts) >= 2:
            return f"{parts[1]}.md"
    except Exception:
        pass
    return "gamefaqs_download.md"


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


def _scrapingbee_html(url: str, key: str) -> str:
    """Fetch a page via the ScrapingBee API (residential IPs)."""
    api_url = "https://app.scrapingbee.com/api/v1/"
    params = {
        "api_key": key,
        "url": url,
        "render_js": "true",
        "premium_proxy": "true",
        "stealth_proxy": "true",
        "return_page_source": "true",
    }
    resp = http_requests.get(api_url, params=params, timeout=120)
    if resp.status_code == 402:
        raise FAQDownloadError("ScrapingBee credits exhausted — check your plan.")
    if resp.status_code == 429:
        raise FAQDownloadError("ScrapingBee rate limited — retry later.")
    resp.raise_for_status()
    return resp.text


def _resolve_game_url(url: str, firecrawl_key: str | None = None,
                      scrapingbee_key: str | None = None) -> str:
    """Given a game page URL, find the best FAQ URL and return it.

    Scrapes the game's FAQ listing page via Firecrawl (or ScrapingBee) to
    avoid direct access blocks from datacenter IPs, then picks the
    highest-rated guide. Returns the original URL if it's already a FAQ URL.
    """
    base_url = url.split("?")[0]
    if RE_FAQ_URL.match(base_url):
        return url

    logger.info("Game page URL detected — looking up its FAQ listing")

    m = re.search(r"gamefaqs\.gamespot\.com/([a-z0-9-]+)/(\d+-[^/?]+)", base_url)
    if not m:
        raise FAQDownloadError(
            f"Could not extract game info from URL: {url}"
        )
    canonical = f"https://gamefaqs.gamespot.com/{m.group(1)}/{m.group(2)}"
    listing_url = canonical + "/faqs"

    guides: list[FAQGuide] = []
    if firecrawl_key:
        try:
            inner = _firecrawl_scrape(listing_url, firecrawl_key)
            guides = _extract_faq_links(inner, listing_url)
        except (RuntimeError, http_requests.RequestException, FAQDownloadError) as exc:
            logger.warning("Firecrawl listing lookup failed: %s", exc)
    elif scrapingbee_key:
        try:
            html = _scrapingbee_html(listing_url, scrapingbee_key)
            seen: set[str] = set()
            for h in re.findall(r'href="([^"]*/faqs/\d+[^"]*)"', html):
                h = re.sub(r"(/faqs/\d+).*", r"\1", h)
                if not h.startswith("http"):
                    h = f"https://gamefaqs.gamespot.com{h}"
                if h in seen:
                    continue
                seen.add(h)
                slug_m = re.search(r"/faqs/\d+-([^/?]+)", h)
                title = slug_m.group(1).replace("-", " ").title() if slug_m else "FAQ"
                guides.append(FAQGuide(title=title, url=h))
        except FAQDownloadError as exc:
            logger.warning("ScrapingBee listing lookup failed: %s", exc)

    if not guides:
        raise FAQDownloadError(
            f"Could not find any FAQ guides for '{m.group(2)}'. "
            f"Try providing a direct FAQ URL instead."
        )

    best = min(guides, key=lambda g: (g.rating_rank, g.title))
    logger.info("Auto-selected FAQ: %s (%s)", best.url, best.title)
    return best.url


class FAQDownloader:
    def __init__(self, url: str, output_dir: str = ".",
                 scrapingbee_key: str | None = None,
                 firecrawl_key: str | None = None) -> None:
        base_url = url.split("?")[0]
        if not _validate_url(url):
            raise FAQDownloadError(
                f"Invalid URL — expected a GameFAQs FAQ or game page URL. Got: {url}"
            )
        self.scrapingbee_key = scrapingbee_key
        self.firecrawl_key = firecrawl_key
        self.url = _resolve_game_url(base_url, firecrawl_key, scrapingbee_key)
        self.url = _ensure_single_param(self.url)
        self.output_dir = os.path.expanduser(output_dir)

    def _commit_title(self) -> str:
        m = re.search(r"gamefaqs\.gamespot\.com/[a-z0-9-]+/\d+-([^/?]+)", self.url)
        if not m:
            return "Add GameFAQs guide"
        return f"Add {m.group(1).replace('-', ' ').title()} guide"

    def fetch_and_save(self, commit_title_path: str | None = None) -> str:
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
        if commit_title_path:
            with open(commit_title_path, "w", encoding="utf-8") as fh:
                fh.write(self._commit_title() + "\n")
            logger.info('Wrote commit title to "%s"', commit_title_path)
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
        logger.info("Trying ScrapingBee for %s", self.url)
        html = _scrapingbee_html(self.url, self.scrapingbee_key)
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
    parser.add_argument(
        "--commit-title", default=None, metavar="FILE",
        help="Write a commit title line (e.g. 'Add <game> guide') to FILE",
    )
    args = parser.parse_args()
    try:
        downloader = FAQDownloader(args.url, args.output,
                                   scrapingbee_key=args.scrapingbee,
                                   firecrawl_key=args.firecrawl)
        filepath = downloader.fetch_and_save(commit_title_path=args.commit_title)
        print(filepath)
    except FAQDownloadError as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
