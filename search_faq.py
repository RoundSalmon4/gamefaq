#!/usr/bin/env python3
"""
Search GameFAQs for guides by game title.

Displays matching games, their platforms, and available FAQ guides
with ratings so you can pick the right URL to download.

Uses Playwright to bypass Cloudflare protections.

Usage:
    python search_faq.py "game title"

Example:
    python search_faq.py "final fantasy vii"
    python search_faq.py "zelda" --console snes
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

NAV_TIMEOUT_MS = 60_000
CLOUDFLARE_POLL_INTERVAL = 0.5
CLOUDFLARE_MAX_WAIT = 15

_CF_CHALLENGE_INDICATORS = (
    "text='Verify you are human'",
    "text='Just a moment'",
    "#challenge-running",
    "#cf-challenge-running",
)

RATING_ORDER = {
    "Highest Rated": 1,
    "Most Recommended": 2,
    "Complete": 3,
    "Partial": 4,
    "Unrated": 5,
}

# Set to True after first "Request Blocked" detection to skip direct GameFAQs
# access for the rest of the session (always blocked from datacenter IPs).
_direct_access_blocked = False


@dataclass
class FAQGuide:
    title: str
    url: str
    rating: str = "Unrated"
    rating_rank: int = 5

    def __str__(self) -> str:
        tag = f" [{self.rating}]" if self.rating != "Unrated" else ""
        return f"  {self.title}{tag}\n    {self.url}"


@dataclass
class GameResult:
    title: str
    platform: str
    url: str
    guides: list[FAQGuide] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.title} ({self.platform})\n  {self.url}"


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
    logger.info("Cloudflare challenge detected - waiting...")
    deadline = time.monotonic() + CLOUDFLARE_MAX_WAIT
    while time.monotonic() < deadline:
        page.wait_for_timeout(int(CLOUDFLARE_POLL_INTERVAL * 1000))
        if not _has_cloudflare_challenge(page):
            logger.info("Cloudflare cleared.")
            return
    logger.warning("Cloudflare did not clear within %d s.", CLOUDFLARE_MAX_WAIT)


def _launch_browser():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-web-security",
        ],
    )
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
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
    return pw, browser, context


def _dump_debug_html(page: Page, path: str = "debug_page.html") -> None:
    """Save the current page HTML for debugging."""
    try:
        html = page.content()
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Debug HTML saved to %s (%d bytes)", path, len(html))
    except Exception as e:
        logger.debug("Failed to save debug HTML: %s", e)


def _parse_gamefaqs_url(href: str) -> tuple[str, str]:
    """Extract (platform, slug) from a GameFAQs URL. Returns ('', '') on failure."""
    m = re.search(r"gamefaqs\.gamespot\.com/([a-z0-9-]+)/(\d+-[^/?]+)", href)
    if m:
        return m.group(1).replace("-", " ").title(), m.group(2)
    return "", ""


def _is_gamefaqs_game_page(href: str) -> bool:
    """Check if a URL looks like a GameFAQs game listing page."""
    return bool(re.search(r"gamefaqs\.gamespot\.com/[a-z0-9-]+/\d+-[^/]+/?$", href))


def _is_gamefaqs_faq_page(href: str) -> bool:
    """Check if a URL looks like a GameFAQs FAQ page (has numeric FAQ ID)."""
    return bool(re.search(r"gamefaqs\.gamespot\.com/.+/faqs/\d+", href))


def _clean_title(raw: str) -> str:
    """Clean up a title extracted from Brave/Startpage link text.

    Link text often contains breadcrumb separators and multiple lines.
    We want the last meaningful line (usually the actual page title).
    """
    # Split on newlines and breadcrumb separators
    parts = re.split(r'[\n\r]+|(?<!\w)•(?!\w)', raw)
    # Filter to parts that look like actual titles (not URLs, not short crumbs)
    candidates = []
    for p in parts:
        p = p.strip()
        if not p or len(p) < 4:
            continue
        # Skip parts that are just domain/path fragments
        if "gamefaqs.gamespot.com" in p.lower():
            continue
        # Skip single-word crumbs like "ps5", "faqs", "introduction"
        if len(p.split()) <= 2 and not any(c.isupper() for c in p[1:]):
            continue
        candidates.append(p)
    # The last candidate is usually the most descriptive title
    if candidates:
        return candidates[-1]
    # Fallback: just strip and take first line
    return raw.strip().split("\n")[0][:120]


def _search_brave(page: Page, query: str) -> None:
    """Navigate to Brave Search and wait for results to load."""
    brave_url = f"https://search.brave.com/search?q={quote_plus(query)}"
    logger.info("Searching Brave: %s", query)
    page.goto(brave_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    # Brave is a SvelteKit app — wait for result elements to appear
    try:
        page.wait_for_selector("a[href*='gamefaqs.gamespot.com'], .result-header, .snippet",
                               timeout=15_000)
    except PlaywrightTimeoutError:
        logger.warning("Timed out waiting for Brave results, continuing anyway...")
    time.sleep(1)


def search_games(query: str, console_filter: str | None = None,
                 debug: bool = False) -> list[GameResult]:
    """Search for GameFAQs guides via Brave Search (GameFAQs + Google both block datacenter IPs)."""
    pw, browser, context = _launch_browser()
    try:
        page = context.new_page()

        brave_query = f"site:gamefaqs.gamespot.com {query}"
        _search_brave(page, brave_query)

        results: list[GameResult] = []
        seen_urls: set[str] = set()

        # Find all GameFAQs links on the page
        all_links = page.locator("a[href*='gamefaqs.gamespot.com']").all()
        logger.info("Found %d links to gamefaqs.gamespot.com", len(all_links))

        for link_el in all_links:
            try:
                href = link_el.get_attribute("href") or ""
                if not href or href in seen_urls:
                    continue

                # Skip boards, search, and other non-game pages
                if any(skip in href for skip in ("/boards/", "/search", "/topic/")):
                    continue

                seen_urls.add(href)

                # Extract platform from URL
                platform, slug = _parse_gamefaqs_url(href)
                if not slug:
                    continue

                # Derive a readable title from the slug
                slug_title = re.sub(r"^\d+-", "", slug).replace("-", " ").title()

                # Try to get the link text as the display title
                title = _clean_title(link_el.inner_text())
                if not title or len(title) < 3 or title.lower() in ("gamefaqs", "gamefaqs.com"):
                    title = slug_title

                # Skip if platform filter doesn't match
                if console_filter and console_filter.upper() not in platform.upper():
                    continue

                if _is_gamefaqs_faq_page(href):
                    # Extract the base game URL from this FAQ sub-page
                    game_base = re.sub(r'/faqs/.*$', '', href).rstrip("/")
                    faq_title = title
                    faq_url = href if href.startswith("http") else f"https://gamefaqs.gamespot.com{href}"
                    if game_base not in seen_urls:
                        # First time seeing this game — add it as a game result
                        results.append(GameResult(
                            title=slug_title,
                            platform=platform,
                            url=game_base,
                            guides=[FAQGuide(title=faq_title, url=faq_url)],
                        ))
                        seen_urls.add(game_base)
                    else:
                        # Already have this game — append this FAQ as a pre-discovered guide
                        for r in results:
                            if r.url == game_base:
                                r.guides.append(FAQGuide(title=faq_title, url=faq_url))
                                break
                elif _is_gamefaqs_game_page(href):
                    if href not in seen_urls:
                        results.append(GameResult(
                            title=title,
                            platform=platform,
                            url=href,
                        ))
                        seen_urls.add(href)
                else:
                    # Some other gamefaqs page (FAQ listing, etc.)
                    if href not in seen_urls:
                        results.append(GameResult(
                            title=title,
                            platform=platform,
                            url=href,
                        ))
                        seen_urls.add(href)

            except Exception as e:
                logger.debug("Error parsing link: %s", e)
                continue

        # Deduplicate by game base URL (keep first occurrence)
        unique: list[GameResult] = []
        deduped_bases: set[str] = set()
        for r in results:
            base = re.sub(r'/faqs/.*$', '', r.url).rstrip("/")
            if base not in deduped_bases:
                deduped_bases.add(base)
                unique.append(r)
        results = unique

        if not results and debug:
            _dump_debug_html(page)

        # If Brave returned nothing, try Startpage as fallback
        if not results:
            logger.info("Brave returned no results, trying Startpage...")
            try:
                brave_query = f"site:gamefaqs.gamespot.com {query}"
                _search_startpage(page, brave_query)

                all_links = page.locator("a[href*='gamefaqs.gamespot.com']").all()
                logger.info("Found %d links via Startpage fallback", len(all_links))

                for link_el in all_links:
                    try:
                        href = link_el.get_attribute("href") or ""
                        if not href or href in seen_urls:
                            continue
                        if any(skip in href for skip in ("/boards/", "/search", "/topic/")):
                            continue
                        seen_urls.add(href)
                        platform, slug = _parse_gamefaqs_url(href)
                        if not slug:
                            continue
                        slug_title = re.sub(r"^\d+-", "", slug).replace("-", " ").title()
                        title = _clean_title(link_el.inner_text())
                        if not title or len(title) < 3 or title.lower() in ("gamefaqs", "gamefaqs.com"):
                            title = slug_title
                        if console_filter and console_filter.upper() not in platform.upper():
                            continue
                        # Collapse FAQ sub-pages to their game base URL
                        if _is_gamefaqs_faq_page(href):
                            game_base = re.sub(r'/faqs/.*$', '', href).rstrip("/")
                            faq_url = href if href.startswith("http") else f"https://gamefaqs.gamespot.com{href}"
                            if game_base not in seen_urls:
                                seen_urls.add(game_base)
                                results.append(GameResult(
                                    title=slug_title, platform=platform, url=game_base,
                                    guides=[FAQGuide(title=title, url=faq_url)],
                                ))
                            else:
                                for r in results:
                                    if r.url == game_base:
                                        r.guides.append(FAQGuide(title=title, url=faq_url))
                                        break
                        else:
                            results.append(GameResult(title=title, platform=platform, url=href))
                    except Exception:
                        continue

                if results:
                    logger.info("Startpage fallback found %d results", len(results))
                    # Deduplicate by game base URL
                    unique2: list[GameResult] = []
                    seen_bases2: set[str] = set()
                    for r in results:
                        base = re.sub(r'/faqs/.*$', '', r.url).rstrip("/")
                        if base not in seen_bases2:
                            seen_bases2.add(base)
                            unique2.append(r)
                    results = unique2

            except Exception as e:
                logger.debug("Startpage fallback failed: %s", e)

        return results[:20]

    finally:
        browser.close()
        pw.stop()


def _fetch_faqs_direct(faq_url: str) -> list[FAQGuide]:
    """Try to fetch FAQ listing directly from GameFAQs."""
    global _direct_access_blocked
    if _direct_access_blocked:
        return []
    pw, browser, context = _launch_browser()
    try:
        page = context.new_page()
        logger.info("Fetching FAQs from: %s", faq_url)

        page.goto(faq_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        _wait_for_cloudflare(page)

        time.sleep(2)

        guides: list[FAQGuide] = []

        contrib_tables = page.locator("table.contrib").all()
        if not contrib_tables:
            contrib_tables = page.locator("table[class*='contrib']").all()

        for table in contrib_tables:
            rows = table.locator("tbody tr").all()
            for row in rows:
                try:
                    title_cell = row.locator("td.ctitle").first
                    if title_cell.count() == 0:
                        continue

                    icon = title_cell.locator("i").first
                    rating = "Unrated"
                    if icon.count() > 0:
                        title_attr = icon.get_attribute("title") or ""
                        if title_attr:
                            rating = title_attr

                    link = title_cell.locator("a").first
                    if link.count() == 0:
                        link = row.locator("td a[href*='/faqs/']").first
                    if link.count() == 0:
                        continue

                    guide_title = link.inner_text().strip()
                    guide_url = link.get_attribute("href") or ""
                    if guide_url and not guide_url.startswith("http"):
                        guide_url = f"https://gamefaqs.gamespot.com{guide_url}"

                    if not guide_title or not guide_url:
                        continue

                    guides.append(FAQGuide(
                        title=guide_title,
                        url=guide_url,
                        rating=rating,
                        rating_rank=RATING_ORDER.get(rating, 5),
                    ))
                except Exception as e:
                    logger.debug("Error parsing FAQ row: %s", e)
                    continue

        if not guides:
            all_links = page.locator("a[href*='/faqs/']").all()
            seen_urls: set[str] = set()
            for link in all_links:
                try:
                    href = link.get_attribute("href") or ""
                    text = link.inner_text().strip()
                    if not text or not href or href in seen_urls:
                        continue
                    if text.lower() in ("faqs", "guide", "guides", "back"):
                        continue
                    seen_urls.add(href)
                    full_url = href if href.startswith("http") else f"https://gamefaqs.gamespot.com{href}"
                    guides.append(FAQGuide(
                        title=text,
                        url=full_url,
                        rating="Unrated",
                        rating_rank=5,
                    ))
                except Exception:
                    continue

        # Check if we got blocked
        page_text = page.inner_text("body")
        if "Request Blocked" in page_text or "abuse from this hosting" in page_text:
            _direct_access_blocked = True
            logger.warning("GameFAQs blocked direct access — skipping for rest of session")
            return []

        guides.sort(key=lambda g: g.rating_rank)
        return guides

    finally:
        browser.close()
        pw.stop()


def _search_faqs_via_brave(game_title: str, platform: str = "",
                           debug: bool = False) -> list[FAQGuide]:
    """Search Brave for GameFAQs FAQ pages for a specific game."""
    pw, browser, context = _launch_browser()
    try:
        page = context.new_page()

        site_part = "site:gamefaqs.gamespot.com/faqs/"
        query = f"{site_part} {game_title}"
        if platform:
            platform_slug = platform.lower().replace(" ", "-")
            query += f" {platform_slug}"

        _search_brave(page, query)

        guides: list[FAQGuide] = []
        seen_urls: set[str] = set()

        all_links = page.locator("a[href*='gamefaqs.gamespot.com/faqs/']").all()
        logger.info("Found %d FAQ links via Brave", len(all_links))

        for link_el in all_links:
            try:
                href = link_el.get_attribute("href") or ""
                if not href or href in seen_urls:
                    continue

                if not re.search(r"/faqs/\d+", href):
                    continue

                seen_urls.add(href)

                title = link_el.inner_text().strip()
                if not title:
                    title = re.sub(r"^\d+-", "", re.search(r"/faqs/\d+-([^/?]+)", href).group(1)).replace("-", " ").title() if re.search(r"/faqs/\d+-([^/?]+)", href) else "FAQ"

                # Try to get snippet for rating inference
                snippet = ""
                try:
                    parent = link_el.locator("xpath=ancestor::div[contains(@class,'snippet') or contains(@class,'result')]").first
                    if parent.count() > 0:
                        snippet = parent.inner_text().strip().lower()
                except Exception:
                    pass

                rating = "Unrated"
                if "highest rated" in snippet or "top rated" in snippet:
                    rating = "Highest Rated"
                elif "most recommended" in snippet:
                    rating = "Most Recommended"
                elif "complete" in snippet:
                    rating = "Complete"
                elif "detailed" in snippet or "full" in snippet:
                    rating = "Complete"

                if not href.startswith("http"):
                    href = f"https://gamefaqs.gamespot.com{href}"

                guides.append(FAQGuide(
                    title=title,
                    url=href,
                    rating=rating,
                    rating_rank=RATING_ORDER.get(rating, 5),
                ))
            except Exception as e:
                logger.debug("Error parsing Brave FAQ result: %s", e)
                continue

        if not guides and debug:
            _dump_debug_html(page, path="debug_brave_faqs.html")

        guides.sort(key=lambda g: g.rating_rank)
        return guides

    finally:
        browser.close()
        pw.stop()


def _search_startpage(page: Page, query: str) -> None:
    """Navigate to Startpage and wait for results to load."""
    startpage_url = f"https://startpage.com/do/dsearch?query={quote_plus(query)}&cat=web"
    logger.info("Searching Startpage: %s", query)
    page.goto(startpage_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    try:
        page.wait_for_selector("a[href*='gamefaqs.gamespot.com'], .result, .w-gl__result",
                               timeout=15_000)
    except PlaywrightTimeoutError:
        logger.warning("Timed out waiting for Startpage results, continuing anyway...")
    time.sleep(1)


def _search_faqs_via_startpage(game_title: str, platform: str = "",
                               debug: bool = False) -> list[FAQGuide]:
    """Search Startpage for GameFAQs FAQ pages for a specific game."""
    pw, browser, context = _launch_browser()
    try:
        page = context.new_page()

        site_part = "site:gamefaqs.gamespot.com/faqs/"
        query = f"{site_part} {game_title}"
        if platform:
            platform_slug = platform.lower().replace(" ", "-")
            query += f" {platform_slug}"

        _search_startpage(page, query)

        guides: list[FAQGuide] = []
        seen_urls: set[str] = set()

        all_links = page.locator("a[href*='gamefaqs.gamespot.com/faqs/']").all()
        logger.info("Found %d FAQ links via Startpage", len(all_links))

        for link_el in all_links:
            try:
                href = link_el.get_attribute("href") or ""
                if not href or href in seen_urls:
                    continue

                if not re.search(r"/faqs/\d+", href):
                    continue

                seen_urls.add(href)

                title = link_el.inner_text().strip()
                if not title or len(title) < 3:
                    slug_m = re.search(r"/faqs/\d+-([^/?]+)", href)
                    if slug_m:
                        title = slug_m.group(1).replace("-", " ").title()
                    else:
                        title = "FAQ"

                if not href.startswith("http"):
                    href = f"https://gamefaqs.gamespot.com{href}"

                guides.append(FAQGuide(
                    title=title,
                    url=href,
                    rating="Unrated",
                    rating_rank=5,
                ))
            except Exception as e:
                logger.debug("Error parsing Startpage FAQ result: %s", e)
                continue

        if not guides and debug:
            _dump_debug_html(page, path="debug_startpage_faqs.html")

        guides.sort(key=lambda g: g.rating_rank)
        return guides

    finally:
        browser.close()
        pw.stop()


def get_faqs(game_url: str, game_title: str = "",
             platform: str = "", debug: bool = False,
             pre_discovered: list[FAQGuide] | None = None) -> list[FAQGuide]:
    """Fetch the FAQ listing for a game. Falls back to Brave/Startpage if blocked."""
    global _direct_access_blocked

    # If the initial search already found FAQ URLs, use those directly
    if pre_discovered:
        logger.info("Using %d pre-discovered FAQ URLs from search", len(pre_discovered))
        return pre_discovered

    # Clean the game title — strip FAQ sub-page noise like "(FAQ: ... Walkthrough...)"
    clean_title = re.sub(r'\s*\(FAQ:.*', '', game_title).strip()
    # Also strip generic suffixes
    clean_title = re.sub(r'\s*(FAQs?, Walkthroughs?,? and Guides? for )', '', clean_title).strip()
    clean_title = re.sub(r'\s*-\s*GameFAQs$', '', clean_title).strip()

    # Extract the base game URL (strip any /faqs/... sub-path)
    base_url = re.sub(r'/faqs/.*$', '', game_url).rstrip("/")
    faq_url = base_url + "/faqs/"

    # Try direct access first (unless already known blocked)
    if not _direct_access_blocked:
        guides = _fetch_faqs_direct(faq_url)
        if guides:
            return guides

    # Direct access blocked — try Brave, then Startpage
    if not _direct_access_blocked:
        logger.info("Direct access blocked, trying search engine fallback...")

    # Use the clean title for search, falling back to URL-derived title
    search_title = clean_title
    if not search_title or len(search_title) < 3:
        gm = re.search(r"/\d+-([^/]+)", base_url)
        if gm:
            search_title = gm.group(1).replace("-", " ").title()

    if not platform:
        pm = re.search(r"gamefaqs\.gamespot\.com/([a-z0-9-]+)/\d+", base_url)
        if pm:
            platform = pm.group(1).replace("-", " ").title()

    guides = _search_faqs_via_brave(search_title, platform, debug)
    if guides:
        return guides

    logger.info("Brave returned nothing, trying Startpage...")
    return _search_faqs_via_startpage(search_title, platform, debug)


def format_markdown(query: str, console_filter: str | None,
                    results: list[GameResult],
                    guides_map: dict[int, list[FAQGuide]] | None = None) -> str:
    """Format search results (and optionally guides) as markdown."""
    lines: list[str] = []
    lines.append(f"# GameFAQs Search: {query}")
    if console_filter:
        lines.append(f"**Platform filter:** {console_filter}")
    lines.append("")
    lines.append(f"Found **{len(results)}** result(s).")
    lines.append("")

    for i, game in enumerate(results, 1):
        lines.append(f"## [{i}] {game.title}")
        lines.append(f"**Platform:** {game.platform}  ")
        lines.append(f"**Game page:** {game.url}")
        lines.append("")

        if guides_map and i in guides_map:
            guides = guides_map[i]
            if guides:
                lines.append("| # | Guide | Rating | URL |")
                lines.append("|---|-------|--------|-----|")
                for j, g in enumerate(guides, 1):
                    lines.append(
                        f"| {j} | {g.title} | {g.rating} | [link]({g.url}) |"
                    )
                lines.append("")
            else:
                lines.append("_No FAQs found for this game._")
                lines.append("")
        else:
            lines.append("_Re-run with `-g {i}` to see available FAQs._")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("### Download a guide")
    lines.append("Copy a URL from above, then trigger the **Download GameFAQ** workflow "
                 "or run locally:")
    lines.append("```")
    lines.append("python download_faq.py <url>")
    lines.append("```")
    lines.append("")
    lines.append("_Works with both game page URLs and direct FAQ links._")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search GameFAQs for guides by game title.",
    )
    parser.add_argument("query", help="Game title to search for")
    parser.add_argument(
        "-c", "--console",
        help="Filter by console/platform (e.g. snes, ps1, gba)",
        default=None,
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="List search results and exit",
    )
    parser.add_argument(
        "-g", "--guides",
        type=int,
        metavar="N",
        help="Show FAQ guides for result #N",
    )
    parser.add_argument(
        "-a", "--all-guides",
        action="store_true",
        help="Fetch FAQ guides for all results",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Output results as markdown (for CI summaries)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save page HTML to debug_page.html when no results found",
    )
    args = parser.parse_args()

    results = search_games(args.query, args.console, debug=args.debug)

    if not results:
        if args.markdown:
            print(f"# GameFAQs Search: {args.query}\n\nNo games found.")
        else:
            print("No games found.")
        sys.exit(1)

    guides_map: dict[int, list[FAQGuide]] = {}

    if args.guides:
        idx = args.guides - 1
        if idx < 0 or idx >= len(results):
            if not args.markdown:
                print(f"\nInvalid selection. Choose 1-{len(results)}.")
            sys.exit(1)
        game = results[idx]
        guides_map[args.guides] = get_faqs(
            game.url, game.title, game.platform, args.debug,
            pre_discovered=game.guides or None,
        )

    if args.all_guides:
        for i, game in enumerate(results, 1):
            logger.info("Fetching guides for [%d] %s...", i, game.title)
            guides_map[i] = get_faqs(
                game.url, game.title, game.platform, args.debug,
                pre_discovered=game.guides or None,
            )

    if args.markdown:
        print(format_markdown(args.query, args.console, results,
                              guides_map if guides_map else None))
        return

    print(f"\n{'='*60}")
    print(f" Search results for: {args.query}")
    if args.console:
        print(f" Platform filter: {args.console}")
    print(f"{'='*60}\n")

    for i, game in enumerate(results, 1):
        print(f"  [{i}] {game}")

    if args.guides:
        idx = args.guides - 1
        chosen = results[idx]
        print(f"\n{'='*60}")
        print(f" FAQs for: {chosen.title} ({chosen.platform})")
        print(f"{'='*60}\n")

        guides = guides_map.get(args.guides, [])
        if not guides:
            print("  No FAQs found for this game.")
            sys.exit(1)

        for i, guide in enumerate(guides, 1):
            print(f"  [{i}] {guide}")

        print(f"\n{'='*60}")
        print(" Copy a URL above and use it with download_faq.py:")
        print("   python download_faq.py <url>")
        print(f"{'='*60}\n")

    elif args.all_guides:
        for i, game in enumerate(results, 1):
            print(f"\n{'='*60}")
            print(f" FAQs for: {game.title} ({game.platform})")
            print(f"{'='*60}\n")
            guides = guides_map.get(i, [])
            if not guides:
                print("  No FAQs found.")
                continue
            for j, guide in enumerate(guides, 1):
                print(f"  [{j}] {guide}")

    elif not args.list:
        print(f"\n  Use -g <number> to view FAQs for a game.")
        print(f"  Example: python search_faq.py \"{args.query}\" -g 1")


if __name__ == "__main__":
    main()
