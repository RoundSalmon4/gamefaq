#!/usr/bin/env python3
"""
Search GameFAQs for guides by game title.

Displays matching games, their platforms, and available FAQ guides
with ratings so you can pick the right URL to download.

Uses requests to search Brave/DuckDuckGo (no browser needed).

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
from urllib.parse import quote_plus

import requests as http_requests

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

RATING_ORDER = {
    "Highest Rated": 1,
    "Most Recommended": 2,
    "Complete": 3,
    "Partial": 4,
    "Unrated": 5,
}

_session: http_requests.Session | None = None


def _get_session() -> http_requests.Session:
    global _session
    if _session is None:
        _session = http_requests.Session()
        _session.headers.update({"User-Agent": USER_AGENT})
    return _session


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
    _relevance: float = field(default=0.0, repr=False, compare=False)

    def __str__(self) -> str:
        score = f" [rel: {self._relevance:.0%}]" if self._relevance else ""
        return f"{self.title}{score} ({self.platform})\n  {self.url}"


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


def _extract_gamefaqs_links(html: str) -> list[str]:
    """Extract all gamefaqs.gamespot.com links from HTML."""
    raw = re.findall(r'href="(https?://gamefaqs\.gamespot\.com[^"]*)"', html)
    # Also catch protocol-relative links
    raw += [f"https://{m}" for m in re.findall(
        r'href="(//gamefaqs\.gamespot\.com[^"]*)"', html
    )]
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for link in raw:
        link = link.split("#")[0].split("?")[0] if "?" in link else link.split("#")[0]
        # Re-append query params for actual href, but keep base for dedup
        base = link.split("?")[0]
        if base not in seen:
            seen.add(base)
            result.append(link)
    return result


def _fetch_search_html(query: str, engine: str = "brave") -> str:
    """Fetch search results HTML from Brave or DuckDuckGo."""
    sess = _get_session()
    if engine == "brave":
        url = f"https://search.brave.com/search?q={quote_plus(query)}"
    else:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        resp = sess.get(url, timeout=30)
        resp.raise_for_status()
        return resp.text
    except http_requests.RequestException as exc:
        logger.warning("Search request failed (%s): %s", engine, exc)
        return ""


def _dump_debug_html(html: str, path: str = "debug_page.html") -> None:
    """Save HTML for debugging."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Debug HTML saved to %s (%d bytes)", path, len(html))
    except Exception as e:
        logger.debug("Failed to save debug HTML: %s", e)


def search_games(query: str, console_filter: str | None = None,
                 debug: bool = False,
                 min_relevance: float = 0.3) -> list[GameResult]:
    """Search for GameFAQs guides via Brave/DuckDuckGo."""
    query_words = set(query.lower().split())
    slug_query = query.lower().replace(" ", "-")

    brave_query = (
        f"site:gamefaqs.gamespot.com \"{query}\" "
        f"-faqs -boards -news -community -qna -answers"
    )
    slug_brave_query = (
        f"site:gamefaqs.gamespot.com {slug_query} "
        f"-faqs -boards -news -community -qna -answers"
    )

    results: list[GameResult] = []
    seen_urls: set[str] = set()

    def _collect_from_html(html: str) -> None:
        nonlocal results, seen_urls
        links = _extract_gamefaqs_links(html)
        logger.info("Found %d links to gamefaqs.gamespot.com", len(links))

        for href in links:
            try:
                if href in seen_urls:
                    continue
                if any(skip in href for skip in ("/boards/", "/search", "/topic/", "/community/")):
                    continue
                seen_urls.add(href)

                platform, slug = _parse_gamefaqs_url(href)
                if not slug:
                    continue

                slug_title = re.sub(r"^\d+-", "", slug).replace("-", " ").title()
                title_words = set(slug_title.lower().split())
                overlap = len(query_words & title_words)
                relevance = overlap / len(query_words) if query_words else 0

                if console_filter and console_filter.upper() not in platform.upper():
                    continue

                if _is_gamefaqs_faq_page(href):
                    game_base = re.sub(r'/faqs/.*$', '', href).rstrip("/")
                    faq_url = href if href.startswith("http") else f"https://gamefaqs.gamespot.com{href}"
                    if game_base not in seen_urls:
                        results.append(GameResult(
                            title=slug_title, platform=platform, url=game_base,
                            guides=[FAQGuide(title=slug_title, url=faq_url)],
                            _relevance=relevance,
                        ))
                        seen_urls.add(game_base)
                    else:
                        for r in results:
                            if r.url == game_base:
                                r.guides.append(FAQGuide(title=slug_title, url=faq_url))
                                break
                elif _is_gamefaqs_game_page(href):
                    if href not in seen_urls:
                        results.append(GameResult(
                            title=slug_title, platform=platform, url=href,
                            _relevance=relevance,
                        ))
                        seen_urls.add(href)
                else:
                    if href not in seen_urls:
                        results.append(GameResult(
                            title=slug_title, platform=platform, url=href,
                            _relevance=relevance,
                        ))
                        seen_urls.add(href)
            except Exception as e:
                logger.debug("Error parsing link: %s", e)
                continue

    # Primary search: Brave
    logger.info("Searching Brave: %s", brave_query)
    html = _fetch_search_html(brave_query, "brave")
    if html:
        _collect_from_html(html)

    # If weak results, try slug-based search
    if not results or all(r._relevance < min_relevance for r in results):
        logger.info("Weak results, trying URL-slug search: %s", slug_query)
        html = _fetch_search_html(slug_brave_query, "brave")
        if html:
            _collect_from_html(html)

    # Filter to relevant results and sort
    relevant = [r for r in results if r._relevance >= min_relevance or len(results) <= 2]
    relevant.sort(key=lambda r: (-r._relevance, r.title))

    # Deduplicate by game base URL
    unique: list[GameResult] = []
    deduped_bases: set[str] = set()
    for r in relevant:
        base = re.sub(r'/faqs/.*$', '', r.url).rstrip("/")
        if base not in deduped_bases:
            deduped_bases.add(base)
            unique.append(r)
    results = unique

    if not results and debug:
        _dump_debug_html(html or "")

    # Fallback: DuckDuckGo if Brave returned nothing
    if not results:
        logger.info("Brave returned no results, trying DuckDuckGo...")
        ddg_query = f"site:gamefaqs.gamespot.com \"{query}\" -faqs -boards -news"
        html = _fetch_search_html(ddg_query, "duckduckgo")
        if html:
            _collect_from_html(html)
            relevant2 = [r for r in results if r._relevance >= min_relevance]
            relevant2.sort(key=lambda r: (-r._relevance, r.title))
            unique2: list[GameResult] = []
            seen_bases2: set[str] = set()
            for r in relevant2:
                base = re.sub(r'/faqs/.*$', '', r.url).rstrip("/")
                if base not in seen_bases2:
                    seen_bases2.add(base)
                    unique2.append(r)
            results = unique2

    return results[:20]


def _search_faqs_via_search_engine(game_title: str, platform: str = "",
                                   debug: bool = False) -> list[FAQGuide]:
    """Search for FAQ pages for a specific game via Brave/DuckDuckGo."""
    site_part = "site:gamefaqs.gamespot.com/faqs/"
    query = f"{site_part} {game_title}"
    if platform:
        platform_slug = platform.lower().replace(" ", "-")
        query += f" {platform_slug}"

    guides: list[FAQGuide] = []
    seen_urls: set[str] = set()

    for engine in ("brave", "duckduckgo"):
        logger.info("Searching %s for FAQs: %s", engine, query)
        html = _fetch_search_html(query, engine)
        if not html:
            continue

        links = re.findall(
            r'href="(https?://gamefaqs\.gamespot\.com[^"]*?/faqs/\d+[^"]*)"',
            html,
        )
        links += [f"https://{m}" for m in re.findall(
            r'href="(//gamefaqs\.gamespot\.com[^"]*?/faqs/\d+[^"]*)"',
            html,
        )]
        logger.info("Found %d FAQ links via %s", len(links), engine)

        for href in links:
            try:
                if href in seen_urls:
                    continue
                if not re.search(r"/faqs/\d+", href):
                    continue
                seen_urls.add(href)

                slug_m = re.search(r"/faqs/\d+-([^/?]+)", href)
                title = slug_m.group(1).replace("-", " ").title() if slug_m else "FAQ"

                # Infer rating from surrounding text
                rating = "Unrated"
                idx = html.find(href)
                if idx >= 0:
                    snippet = html[max(0, idx - 500):idx + 500].lower()
                    if "highest rated" in snippet or "top rated" in snippet:
                        rating = "Highest Rated"
                    elif "most recommended" in snippet:
                        rating = "Most Recommended"
                    elif "complete" in snippet:
                        rating = "Complete"

                if not href.startswith("http"):
                    href = f"https://gamefaqs.gamespot.com{href}"

                guides.append(FAQGuide(
                    title=title, url=href,
                    rating=rating,
                    rating_rank=RATING_ORDER.get(rating, 5),
                ))
            except Exception as e:
                logger.debug("Error parsing FAQ result: %s", e)
                continue

        if guides:
            break

    guides.sort(key=lambda g: g.rating_rank)
    return guides


def get_faqs(game_url: str, game_title: str = "",
             platform: str = "", debug: bool = False,
             pre_discovered: list[FAQGuide] | None = None) -> list[FAQGuide]:
    """Fetch the FAQ listing for a game via search engines."""
    if pre_discovered:
        logger.info("Using %d pre-discovered FAQ URLs from search", len(pre_discovered))
        return pre_discovered

    clean_title = re.sub(r'\s*\(FAQ:.*', '', game_title).strip()
    clean_title = re.sub(r'\s*(FAQs?, Walkthroughs?,? and Guides? for )', '', clean_title).strip()
    clean_title = re.sub(r'\s*-\s*GameFAQs$', '', clean_title).strip()

    base_url = re.sub(r'/faqs/.*$', '', game_url).rstrip("/")

    search_title = clean_title
    if not search_title or len(search_title) < 3:
        gm = re.search(r"/\d+-([^/]+)", base_url)
        if gm:
            search_title = gm.group(1).replace("-", " ").title()

    if not platform:
        pm = re.search(r"gamefaqs\.gamespot\.com/([a-z0-9-]+)/\d+", base_url)
        if pm:
            platform = pm.group(1).replace("-", " ").title()

    return _search_faqs_via_search_engine(search_title, platform, debug)


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
        rel_tag = f" (rel: {game._relevance:.0%})" if game._relevance else ""
        lines.append(f"## [{i}] {game.title}{rel_tag}")
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
    parser.add_argument(
        "--min-relevance", type=float, default=0.3, metavar="FLOAT",
        help="Minimum relevance threshold (0.0-1.0, default 0.3)",
    )
    args = parser.parse_args()

    results = search_games(args.query, args.console, debug=args.debug,
                           min_relevance=args.min_relevance)

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
