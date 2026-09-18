#!/usr/bin/env python3
"""
Search GameFAQs for guides by game title.

Displays matching games, their platforms, and available FAQ guides
with ratings so you can pick the right URL to download.

Uses the Firecrawl Search API (managed infrastructure) so the search
works from any network without being blocked by search engines or
datacenter IP filters.

Usage:
    python search_faq.py "game title" --firecrawl KEY

Example:
    python search_faq.py "final fantasy vii"
    python search_faq.py "zelda" --console snes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests as http_requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"

# Free tier rate limit is 10 requests/min combined for /scrape and /search.
MIN_INTERVAL_BETWEEN_REQUESTS = 6.5  # seconds

RATING_ORDER = {
    "Highest Rated": 1,
    "Most Recommended": 2,
    "Complete": 3,
    "Partial": 4,
    "Unrated": 5,
}

_last_request_ts = 0.0


def _pace_request() -> None:
    """Sleep to keep behind the free-tier 10 req/min combined rate limit."""
    global _last_request_ts
    elapsed = time.time() - _last_request_ts
    if elapsed < MIN_INTERVAL_BETWEEN_REQUESTS:
        time.sleep(MIN_INTERVAL_BETWEEN_REQUESTS - elapsed)
    _last_request_ts = time.time()


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


def _firecrawl_search(query: str, api_key: str, limit: int = 10) -> list[dict]:
    """Run a Firecrawl search restricted to GameFAQs. Returns result dicts."""
    payload = {
        "query": query,
        "includeDomains": ["gamefaqs.gamespot.com"],
        "limit": limit,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    _pace_request()
    resp = http_requests.post(
        FIRECRAWL_SEARCH_URL, headers=headers, json=payload, timeout=120
    )
    if resp.status_code == 402:
        raise RuntimeError(
            "Firecrawl credits exhausted - check your plan."
        )
    if resp.status_code == 429:
        raise RuntimeError("Firecrawl rate limited - retry later.")
    resp.raise_for_status()

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Firecrawl search error: {data.get('error', 'unknown')}")

    inner = data.get("data", {})
    web = inner.get("web") or []
    logger.info(
        "Firecrawl search returned %d result(s) (creditsUsed=%d)",
        len(web),
        data.get("creditsUsed", "?"),
    )
    return web


def _firecrawl_scrape(url: str, api_key: str) -> dict:
    """Scrape a page via Firecrawl, returning the inner data dict."""
    payload = {
        "url": url,
        "formats": ["markdown", "links"],
        "onlyMainContent": False,
        "removeBase64Images": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    logger.info("Scraping %s via Firecrawl", url)
    _pace_request()
    resp = http_requests.post(
        FIRECRAWL_SCRAPE_URL, headers=headers, json=payload, timeout=120
    )
    if resp.status_code == 402:
        raise RuntimeError("Firecrawl credits exhausted - check your plan.")
    if resp.status_code == 429:
        raise RuntimeError("Firecrawl rate limited - retry later.")
    resp.raise_for_status()

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Firecrawl scrape error: {data.get('error', 'unknown')}")

    inner = data.get("data", {})
    logger.info("Firecrawl scrape returned (%d chars markdown)", len(inner.get("markdown") or ""))
    return inner


def _parse_gamefaqs_url(href: str) -> tuple[str, str]:
    """Extract (platform_slug, full_slug) from a GameFAQs URL.
    Returns ('', '') on failure."""
    m = re.search(r"gamefaqs\.gamespot\.com/([a-z0-9-]+)/(\d+-[^/?]+)", href)
    if m:
        return m.group(1), m.group(2)
    return "", ""


def _canonical_game_url(href: str) -> str | None:
    """Normalize any GameFAQs URL down to the game page base URL:
    https://gamefaqs.gamespot.com/<platform>/<gameid>-<slug>"""
    platform, slug = _parse_gamefaqs_url(href)
    if not slug:
        return None
    return f"https://gamefaqs.gamespot.com/{platform}/{slug}"


def _is_gamefaqs_faq_page(href: str) -> bool:
    """Check if a URL looks like a GameFAQs FAQ page (has numeric FAQ ID)."""
    return bool(re.search(r"gamefaqs\.gamespot\.com/.+/faqs/\d+", href))


def _slug_title(slug: str) -> str:
    """Convert a GameFAQs slug (e.g. '300976-monster-hunter-rise') to a title."""
    return re.sub(r"^\d+-", "", slug).replace("-", " ").title()


def _dump_debug_json(obj, path: str = "debug_page.json") -> None:
    """Save raw search/scrape data for debugging."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, default=str)
        logger.info("Debug JSON saved to %s", path)
    except Exception as e:
        logger.debug("Failed to save debug JSON: %s", e)


def search_games(query: str, console_filter: str | None = None,
                 api_key: str | None = None,
                 debug: bool = False,
                 min_relevance: float = 0.3) -> list[GameResult]:
    """Search for GameFAQs games via the Firecrawl Search API."""
    if not api_key:
        logger.error(
            "No Firecrawl API key. Set FIRECRAWL_API_KEY or pass --firecrawl KEY."
        )
        return []

    query_words = set(query.lower().split())

    queries = [query]
    # A bare title can match partial pages; retry with an exact phrase if needed.
    if " " in query.strip():
        queries.append(f'"{query}"')

    games: list[GameResult] = []
    seen_bases: set[str] = set()
    last_results: list[dict] = []

    for q in queries:
        if games and all(g._relevance >= min_relevance for g in games):
            break
        logger.info("Firecrawl search: %r", q)
        try:
            results = _firecrawl_search(q, api_key, limit=10)
        except RuntimeError as exc:
            logger.error("Firecrawl search failed: %s", exc)
            if debug:
                _dump_debug_json({"error": str(exc)})
            return []
        except http_requests.RequestException as exc:
            logger.error("Firecrawl search request failed: %s", exc)
            return []

        last_results = results

        for item in results:
            href = item.get("url", "")
            if not href or "gamefaqs.gamespot.com" not in href:
                continue
            if any(skip in href for skip in ("/boards/", "/search", "/topic/", "/community/")):
                continue

            platform_slug, slug = _parse_gamefaqs_url(href)
            if not slug:
                continue
            platform = platform_slug.replace("-", " ").title()
            game_base = _canonical_game_url(href) or f"https://gamefaqs.gamespot.com/{platform_slug}/{slug}"

            slug_title = _slug_title(slug)
            title_words = set(slug_title.lower().split())
            overlap = len(query_words & title_words)
            relevance = overlap / len(query_words) if query_words else 0

            if console_filter and console_filter.upper() not in platform.upper():
                continue

            def _faq_title(h: str) -> str:
                return _slug_title(re.sub(r".*/faqs/\d+-", "", h))

            if _is_gamefaqs_faq_page(href):
                if game_base not in seen_bases:
                    games.append(GameResult(
                        title=slug_title, platform=platform, url=game_base,
                        guides=[FAQGuide(title=_faq_title(href), url=href)],
                        _relevance=relevance,
                    ))
                    seen_bases.add(game_base)
                else:
                    for g in games:
                        if g.url == game_base:
                            g.guides.append(FAQGuide(title=_faq_title(href), url=href))
                            break
            else:
                if game_base not in seen_bases:
                    games.append(GameResult(
                        title=slug_title, platform=platform, url=game_base,
                        _relevance=relevance,
                    ))
                    seen_bases.add(game_base)

    if not games and debug:
        _dump_debug_json(last_results)

    relevant = [g for g in games if g._relevance >= min_relevance or len(games) <= 2]
    relevant.sort(key=lambda g: (-g._relevance, g.title))
    return relevant[:20]


def _extract_faq_links(inner: dict, base_url: str) -> list[FAQGuide]:
    """Extract FAQ guides from scraped game page data.

    Prefers the real guide title from the page markdown (the link text),
    falling back to the URL slug when the page only exposes links.
    """
    markdown = inner.get("markdown") or ""
    raw_links = inner.get("links") or []

    faq_urls: list[tuple[str, str]] = []  # (absolute_url, title_from_markdown or "")

    # Titles come from markdown links: [Title](url)
    seen_md: set[str] = set()
    for m in re.finditer(
        r"\[([^\]]{1,150})\]\(((?:https?://gamefaqs\.gamespot\.com|/)[^)\s]*?/faqs/\d+[^)\s]*)\)",
        markdown,
    ):
        title = m.group(1).strip()
        url = m.group(2)
        if not re.search(r"/faqs/\d+", url):
            continue
        if "gamefaqs.gamespot.com" not in url:
            url = urljoin(base_url, url)
        if url in seen_md:
            continue
        seen_md.add(url)
        faq_urls.append((url, title))

    # Any /faqs/ links from the links array not already covered
    for href in raw_links:
        if not re.search(r"/faqs/\d+", href):
            continue
        if "gamefaqs.gamespot.com" not in href:
            href = urljoin(base_url, href)
        if href in seen_md:
            continue
        seen_md.add(href)
        faq_urls.append((href, ""))

    def _rating_for(markdown: str, pos: int) -> tuple[int, str]:
        window = (markdown[max(0, pos - 350):pos] + " " + markdown[pos:pos + 250]).lower()
        best: tuple[int, str] = (5, "Unrated")
        best_idx = len(window)
        for word, rank in sorted(RATING_ORDER.items(), key=lambda kv: kv[1]):
            idx = window.find(word.lower())
            if idx != -1 and idx < best_idx:
                best = (rank, word)
                best_idx = idx
        return best

    guides: list[FAQGuide] = []
    added: set[str] = set()

    for url, md_title in faq_urls:
        if url in added:
            continue
        added.add(url)

        title = md_title or _slug_title(
            re.search(r"/faqs/\d+-([^/]+)", url).group(1) if re.search(r"/faqs/\d+-[^/]+", url) else ""
        )

        idx = markdown.find(url)
        if idx == -1:
            # relative link form may not be in markdown text directly
            rel = url[len(base_url.rstrip("/")):] if url.startswith(base_url) else None
            idx = markdown.find(rel) if rel else -1
        rank, rating = _rating_for(markdown, idx) if idx >= 0 else (5, "Unrated")

        guides.append(FAQGuide(title=title, url=url, rating=rating, rating_rank=rank))

    guides.sort(key=lambda g: g.rating_rank)
    return guides


def get_faqs(game_url: str, game_title: str = "",
             platform: str = "", debug: bool = False,
             pre_discovered: list[FAQGuide] | None = None,
             api_key: str | None = None,
             debug_index: int = 0) -> list[FAQGuide]:
    """Fetch the FAQ listing for a game via Firecrawl."""
    if pre_discovered:
        logger.info("Using %d pre-discovered FAQ URLs from search", len(pre_discovered))
        return pre_discovered
    if not api_key:
        return []

    base_url = _canonical_game_url(game_url)
    if not base_url:
        logger.error("Could not normalize game URL: %s", game_url)
        return []

    faq_listing_url = base_url + "/faqs"

    inner: dict = {}
    try:
        inner = _firecrawl_scrape(faq_listing_url, api_key)
    except (RuntimeError, http_requests.RequestException) as exc:
        logger.error("Failed to scrape FAQ listing: %s", exc)
        if debug:
            _dump_debug_json({"url": faq_listing_url, "error": str(exc)},
                             f"debug_faqs_{debug_index}.json")
        return []

    guides = _extract_faq_links(inner, faq_listing_url)
    guides.sort(key=lambda g: g.rating_rank)

    if guides:
        return guides

    if debug:
        _dump_debug_json(inner, f"debug_faqs_{debug_index}.json")

    # Listing came back without FAQ links - fall back to a search for this game.
    logger.info("No FAQ links in listing scrape, searching for FAQ pages...")
    slug = re.search(r"/\d+-([^/]+)$", base_url)
    search_term = slug.group(1).replace("-", " ").title() if slug else game_title
    q = f'{search_term} site:gamefaqs.gamespot.com/faqs'
    try:
        results = _firecrawl_search(q, api_key, limit=10)
    except (RuntimeError, http_requests.RequestException) as exc:
        logger.error("Fallback FAQ search failed: %s", exc)
        return []

    platform_slug, game_slug = _parse_gamefaqs_url(base_url)
    faq_results: list[FAQGuide] = []
    for item in results:
        url = item.get("url", "")
        if not re.search(r"/faqs/\d+", url):
            continue
        if platform_slug and game_slug and f"{platform_slug}/{game_slug}" not in url:
            continue
        if "gamefaqs.gamespot.com" not in url:
            continue
        title = _slug_title(re.sub(r".*/faqs/\d+-", "", url))
        faq_results.append(FAQGuide(title=title, url=url))

    logger.info("Fallback search found %d FAQ page(s)", len(faq_results))
    return faq_results


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
        help="Save raw search data to debug_page.json when no results found",
    )
    parser.add_argument(
        "--min-relevance", type=float, default=0.3, metavar="FLOAT",
        help="Minimum relevance threshold (0.0-1.0, default 0.3)",
    )
    parser.add_argument(
        "--firecrawl", "--firecrawl-key", default=None, metavar="KEY",
        help="Firecrawl API key (uses FIRECRAWL_API_KEY env var if not given)",
    )
    args = parser.parse_args()

    api_key = args.firecrawl or os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        print("Error: no Firecrawl API key. Set FIRECRAWL_API_KEY or pass --firecrawl KEY.")
        sys.exit(1)

    results = search_games(args.query, args.console, api_key=api_key,
                           debug=args.debug,
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
            api_key=api_key,
            debug_index=args.guides,
        )

    if args.all_guides:
        for i, game in enumerate(results, 1):
            logger.info("Fetching guides for [%d] %s...", i, game.title)
            guides_map[i] = get_faqs(
                game.url, game.title, game.platform, args.debug,
                pre_discovered=game.guides or None,
                api_key=api_key,
                debug_index=i,
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