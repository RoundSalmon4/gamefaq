#!/usr/bin/env python3
"""
Download a GameFAQs.com FAQ in plain text format.

Uses Firecrawl to bypass Cloudflare protections.

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
        # Site navigation/UI chrome that leaks into scraped markdown
        r"^\s*\*?[Bb]ookmark\*?\s*$",
        r"^\s*Jump to:\s*$",
        r"^\s*Message Sent\s*$",
        r"Would you recommend this Guide\?",
        r"SendSkipHide",
        r"Close X",
        r"What do you need help on",
        r"^\[\s*Log In\]",
        r"^\[\s*Sign Up\]",
        r"^\s*[-*]?\s*\[Next[^\]]*\]\(https://gamefaqs\.gamespot\.com/",
        r"^\s*[-*]?\s*\[Previous[^\]]*\]\(https://gamefaqs\.gamespot\.com/",
        r"^\s*[-*]?\s*Next:",
        r"^\s*[-*]?\s*Previous:",
        r"^Menu\s*$",
        r"^Back Button\s*$",
        r"^ApplyCancel\s*$",
        r"^Reject AllConfirm My Choices\s*$",
        r"^ConsentLeg\.Interest\s*$",
        r"^checkbox labellabel",
        r"^Search Icon\s*$",
        r"^Filter Icon\s*$",
        r"^Clear\s*$",
        r"Powered by Onetrust",
        r"cdn\.cookielaw\.org",
        r"!\[Company Logo\]",
        r"^\|\s*\|$",
        r"^\|\s*---?\s*\|$",
        r"!\[\]\(https://gamefaqs\.gamespot\.com/ffaq/",
    )
]


def _clean_content(text: str) -> str:
    lines = text.split("\n")

    # Drop everything before the guide's own title heading.
    for i, line in enumerate(lines):
        if re.match(r"^#{1,6}\s+\S", line):
            lines = lines[i:]
            break

    # Cut the footer at the site's cookie/preference widgets.
    for i, line in enumerate(lines):
        if re.match(
            r"^#{1,4}\s*(Privacy Preference Center|Manage Consent|Cookie List)\s*$",
            line,
            re.IGNORECASE,
        ):
            lines = lines[:i]
            break

    cleaned: list[str] = []
    prev_was_junk = False
    blank_streak = 0
    for line in lines:
        line = line.rstrip()
        if any(pat.search(line) for pat in _JUNK_PATTERNS):
            prev_was_junk = True
            blank_streak = 0
            continue
        if not line.strip():
            blank_streak += 1
            if prev_was_junk or blank_streak > 1:
                continue
            cleaned.append("")
            continue
        prev_was_junk = False
        blank_streak = 0
        cleaned.append(line)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def _rewrite_chapter_links(markdown: str) -> str:
    """Rewrite GameFAQs chapter links to local anchors so the downloaded
    guide navigates within itself instead of back to the site.

    Matches the link text to a heading in the same file and links to the
    GitHub-style anchor for that heading (same slug rules the site uses).
    """
    heading_entries: list[tuple[str, str]] = []  # (anchor, raw_heading_text)
    counts: dict[str, int] = {}
    for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", markdown, re.M):
        raw = m.group(1).strip()
        base = re.sub(r"[^\w\s-]", "", raw.lower()).replace(" ", "-")
        n = counts.get(base, 0)
        counts[base] = n + 1
        heading_entries.append((base if n == 0 else f"{base}-{n}", raw))

    def _norm(text: str) -> str:
        text = re.sub(r"^(?:Next|Previous):\s*", "", text).strip()
        text = re.sub(r"[*_`]", "", text)
        text = re.sub(r"['\u2019]", "", text)
        text = re.sub(r"[^\w\s-]", " ", text)
        text = re.sub(r"[-]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text.lower()

    by_text: dict[str, str] = {}
    for anchor, raw in heading_entries:
        by_text.setdefault(_norm(raw), anchor)

    def _repl(m: re.Match) -> str:
        text, url = m.group(1), m.group(2)
        if not re.search(r"gamefaqs\.gamespot\.com/.+/faqs/\d+/", url):
            return m.group(0)
        ntext = _norm(text)
        if ntext in by_text:
            return f"[{text}](#{by_text[ntext]})"
        slug_m = re.search(r"/faqs/\d+/([a-z0-9-]+)", url)
        if slug_m:
            target = _norm(slug_m.group(1).replace("-", " "))
            for anchor, raw in heading_entries:
                if _norm(raw) == target:
                    return f"[{text}](#{anchor})"
        return m.group(0)

    return re.sub(
        r"\[([^\]]{1,150})\]\(([^)\s]*?gamefaqs\.gamespot\.com[^)\s]*?/faqs/\d+/[^)\s]*)\)",
        _repl,
        markdown,
    )


class FetchResult(NamedTuple):
    content: str


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
    """Name the output file after the game slug, e.g. final-fantasy-vii-remake-intergrade.md"""
    try:
        m = re.search(r"gamefaqs\.gamespot\.com/[a-z0-9-]+/(\d+-[^/?]+)", url)
        if m:
            return f"{re.sub(r'^\d+-', '', m.group(1))}.md"
    except Exception:
        pass
    return "gamefaqs_download.md"


def _resolve_game_url(url: str, firecrawl_key: str | None = None) -> str:
    """Given a game page URL, find the best FAQ URL and return it.

    Scrapes the game's FAQ listing page via Firecrawl to avoid direct
    access blocks from datacenter IPs, then picks the highest-rated guide.
    Returns the original URL if it's already a FAQ URL.
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
    else:
        raise FAQDownloadError(
            "No Firecrawl API key - set FIRECRAWL_API_KEY or pass --firecrawl KEY."
        )

    if not guides:
        raise FAQDownloadError(
            f"Could not find any FAQ guides for '{m.group(2)}'. "
            f"Try providing a direct FAQ URL instead."
        )

    # Page order is GameFAQs' rating order (highest rated first).
    # Skip guides flagged incomplete when a completed guide exists.
    best = next((g for g in guides if "Incomplete" not in g.notes), guides[0])
    logger.info("Auto-selected FAQ: %s (%s)", best.url, best.title)
    return best.url


class FAQDownloader:
    def __init__(self, url: str, output_dir: str = ".",
                 firecrawl_key: str | None = None) -> None:
        base_url = url.split("?")[0]
        if not _validate_url(url):
            raise FAQDownloadError(
                f"Invalid URL — expected a GameFAQs FAQ or game page URL. Got: {url}"
            )
        self.firecrawl_key = firecrawl_key
        self.url = _resolve_game_url(base_url, firecrawl_key)
        self.url = _ensure_single_param(self.url)
        self.output_dir = os.path.expanduser(output_dir)

    def _commit_title(self) -> str:
        m = re.search(r"gamefaqs\.gamespot\.com/[a-z0-9-]+/\d+-([^/?]+)", self.url)
        if not m:
            return "Add GameFAQs guide"
        return f"Add {m.group(1).replace('-', ' ').title()} guide"

    def fetch_and_save(self, commit_title_path: str | None = None) -> str:
        result = self._fetch_with_retries()
        text = _clean_content(result.content)
        text = _rewrite_chapter_links(text)
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
        if not self.firecrawl_key:
            raise FAQDownloadError(
                "No Firecrawl API key - set FIRECRAWL_API_KEY or pass --firecrawl KEY."
            )
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self._fetch_content_firecrawl()
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
        return FetchResult(content=markdown)


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
                                   firecrawl_key=args.firecrawl)
        filepath = downloader.fetch_and_save(commit_title_path=args.commit_title)
        print(filepath)
    except FAQDownloadError as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
