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


def _dismiss_google_consent(page: Page) -> None:
    """Dismiss Google's cookie consent dialog if present."""
    try:
        consent_btns = [
            "button:has-text('Accept all')",
            "button:has-text('I agree')",
            "button:has-text('Reject all')",
            "[aria-label='Accept all']",
            "[aria-label='Reject all']",
        ]
        for sel in consent_btns:
            btn = page.locator(sel).first
            if btn.count() > 0:
                btn.click(timeout=3000)
                page.wait_for_timeout(1000)
                return
    except Exception:
        pass


def search_games(query: str, console_filter: str | None = None,
                 debug: bool = False) -> list[GameResult]:
    """Search for GameFAQs guides via Google (GameFAQs blocks datacenter IPs)."""
    pw, browser, context = _launch_browser()
    try:
        page = context.new_page()

        # Search Google for GameFAQs results
        google_query = f"site:gamefaqs.gamespot.com {query}"
        google_url = f"https://www.google.com/search?q={quote_plus(google_query)}&num=20"
        logger.info("Searching Google: %s", google_query)

        page.goto(google_url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        _dismiss_google_consent(page)
        time.sleep(1)

        results: list[GameResult] = []
        seen_urls: set[str] = set()

        # Parse Google search results
        search_items = page.locator("#search .g, #rso .g").all()
        if not search_items:
            search_items = page.locator("[data-sokoban-container]").all()
        if not search_items:
            search_items = page.locator(".tF2Cxc, .MjjYud").all()

        logger.info("Found %d Google result containers", len(search_items))

        for item in search_items:
            try:
                # Find the main link
                link = item.locator("a[href*='gamefaqs.gamespot.com']").first
                if link.count() == 0:
                    continue

                href = link.get_attribute("href") or ""
                if not href or href in seen_urls:
                    continue

                # Skip non-game pages
                if "/faqs/" in href and "/faqs/" == href.split("gamefaqs.gamespot.com")[-1][:6]:
                    # This is a direct FAQ link - good!
                    pass
                elif "/search" in href or "/boards/" in href:
                    continue

                seen_urls.add(href)

                # Extract snippet text for context
                snippet = ""
                snippet_el = item.locator(".VwiC3b, .IsZvec, [data-content-feature]").first
                if snippet_el.count() > 0:
                    snippet = snippet_el.inner_text().strip()

                # Parse title from the link or Google result title
                title = link.inner_text().strip()
                if not title or len(title) < 2:
                    title_el = item.locator("h3").first
                    if title_el.count() > 0:
                        title = title_el.inner_text().strip()

                if not title:
                    continue

                # Extract platform from URL
                platform = ""
                platform_match = re.search(
                    r"gamefaqs\.gamespot\.com/([a-z0-9-]+)/(\d+-)", href
                )
                if platform_match:
                    platform = platform_match.group(1).replace("-", " ").title()

                # Clean up Google redirect URLs
                if href.startswith("/url?"):
                    actual_url = re.search(r"[?&]q=([^&]+)", href)
                    if actual_url:
                        from urllib.parse import unquote
                        href = unquote(actual_url.group(1))

                if console_filter and console_filter.upper() not in platform.upper():
                    continue

                # Determine if this is a game page or a direct FAQ link
                is_faq = "/faqs/" in href and re.search(r"/faqs/\d+", href)
                game_match = re.search(r"/(\d+-[^/]+?)(?:/faqs)?/?$", href)

                if is_faq:
                    # Direct FAQ link - extract game info from URL
                    game_slug = ""
                    gm = re.search(r"/[a-z0-9-]+/(\d+-[^/]+)/faqs", href)
                    if gm:
                        game_slug = gm.group(1)
                        game_title_raw = re.sub(r"^\d+-", "", game_slug).replace("-", " ").title()
                    else:
                        game_title_raw = title

                    results.append(GameResult(
                        title=f"{game_title_raw} (FAQ: {title})",
                        platform=platform,
                        url=href,
                    ))
                elif game_match:
                    # Game page
                    game_slug = game_match.group(1)
                    game_title = re.sub(r"^\d+-", "", game_slug).replace("-", " ").title()
                    if not title or title.lower() in ("gamefaqs", "gamefaqs.com"):
                        title = game_title

                    results.append(GameResult(
                        title=title,
                        platform=platform,
                        url=href,
                    ))
                else:
                    # Some other GameFAQs link
                    results.append(GameResult(
                        title=title,
                        platform=platform,
                        url=href,
                    ))

            except Exception as e:
                logger.debug("Error parsing Google result: %s", e)
                continue

        # If no structured results, scan all links
        if not results:
            logger.info("Trying link scan fallback...")
            all_links = page.locator("a[href*='gamefaqs.gamespot.com']").all()
            for link in all_links:
                try:
                    href = link.get_attribute("href") or ""
                    text = link.inner_text().strip()
                    if not href or not text or href in seen_urls:
                        continue
                    if "/search" in href or "/boards/" in href:
                        continue
                    if len(text) < 3:
                        continue

                    # Clean Google redirect
                    if href.startswith("/url?"):
                        actual = re.search(r"[?&]q=([^&]+)", href)
                        if actual:
                            from urllib.parse import unquote
                            href = unquote(actual.group(1))

                    if "gamefaqs.gamespot.com" not in href:
                        continue

                    seen_urls.add(href)

                    platform = ""
                    pm = re.search(r"gamefaqs\.gamespot\.com/([a-z0-9-]+)/\d+", href)
                    if pm:
                        platform = pm.group(1).replace("-", " ").title()

                    if console_filter and console_filter.upper() not in platform.upper():
                        continue

                    results.append(GameResult(
                        title=text,
                        platform=platform,
                        url=href,
                    ))
                except Exception:
                    continue

        if not results and debug:
            _dump_debug_html(page)

        return results[:20]

    finally:
        browser.close()
        pw.stop()


def _fetch_faqs_direct(faq_url: str) -> list[FAQGuide]:
    """Try to fetch FAQ listing directly from GameFAQs."""
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
            logger.warning("GameFAQs blocked direct access (datacenter IP detected)")
            return []

        guides.sort(key=lambda g: g.rating_rank)
        return guides

    finally:
        browser.close()
        pw.stop()


def _search_faqs_via_google(game_title: str, platform: str = "",
                            debug: bool = False) -> list[FAQGuide]:
    """Search Google for GameFAQs FAQ pages for a specific game."""
    pw, browser, context = _launch_browser()
    try:
        page = context.new_page()

        # Build a targeted search for FAQ pages
        site_part = "site:gamefaqs.gamespot.com/faqs/"
        query = f"{site_part} {game_title}"
        if platform:
            platform_slug = platform.lower().replace(" ", "-")
            query += f" {platform_slug}"

        google_url = f"https://www.google.com/search?q={quote_plus(query)}&num=10"
        logger.info("Searching Google for FAQs: %s", query)

        page.goto(google_url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        _dismiss_google_consent(page)
        time.sleep(1)

        guides: list[FAQGuide] = []
        seen_urls: set[str] = set()

        # Parse Google results
        search_items = page.locator("#search .g, #rso .g").all()
        if not search_items:
            search_items = page.locator(".tF2Cxc, .MjjYud").all()

        for item in search_items:
            try:
                link = item.locator("a[href*='gamefaqs.gamespot.com/faqs/']").first
                if link.count() == 0:
                    continue

                href = link.get_attribute("href") or ""
                if not href or href in seen_urls:
                    continue

                # Clean Google redirect
                if href.startswith("/url?"):
                    actual = re.search(r"[?&]q=([^&]+)", href)
                    if actual:
                        from urllib.parse import unquote
                        href = unquote(actual.group(1))

                if "gamefaqs.gamespot.com/faqs/" not in href:
                    continue

                # Must be a direct FAQ page (has a numeric ID)
                if not re.search(r"/faqs/\d+", href):
                    continue

                seen_urls.add(href)

                # Get title from Google result
                title = link.inner_text().strip()
                if not title:
                    h3 = item.locator("h3").first
                    if h3.count() > 0:
                        title = h3.inner_text().strip()

                if not title:
                    continue

                # Try to extract snippet for rating info
                snippet = ""
                snippet_el = item.locator(".VwiC3b, .IsZvec").first
                if snippet_el.count() > 0:
                    snippet = snippet_el.inner_text().strip().lower()

                # Infer rating from snippet keywords
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
                logger.debug("Error parsing Google FAQ result: %s", e)
                continue

        if not guides and debug:
            _dump_debug_html(page, path="debug_google_faqs.html")

        guides.sort(key=lambda g: g.rating_rank)
        return guides

    finally:
        browser.close()
        pw.stop()


def get_faqs(game_url: str, game_title: str = "",
             platform: str = "", debug: bool = False) -> list[FAQGuide]:
    """Fetch the FAQ listing for a game. Falls back to Google if GameFAQs blocks direct access."""
    faq_url = game_url.rstrip("/") + "/faqs/"

    # Try direct access first
    guides = _fetch_faqs_direct(faq_url)
    if guides:
        return guides

    # Direct access failed — fall back to Google search
    logger.info("Direct access blocked, trying Google search for FAQs...")
    if not game_title:
        # Extract title from URL
        gm = re.search(r"/\d+-([^/]+)", game_url)
        if gm:
            game_title = gm.group(1).replace("-", " ").title()

    return _search_faqs_via_google(game_title, platform, debug)


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
            game.url, game.title, game.platform, args.debug
        )

    if args.all_guides:
        for i, game in enumerate(results, 1):
            logger.info("Fetching guides for [%d] %s...", i, game.title)
            guides_map[i] = get_faqs(
                game.url, game.title, game.platform, args.debug
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
