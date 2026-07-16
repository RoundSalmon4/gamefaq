# gamefaq

Search and download GameFAQs guides as markdown.

GameFAQs blocks VPN users and has Cloudflare protections, so this project uses Firecrawl and ScrapingBee to bypass those restrictions in CI. Search uses Brave Search via Playwright to avoid direct access blocks.

## Workflow

1. **Search** for a game and browse available guides
2. **Copy** the guide URL you want (game page URLs work too)
3. **Download** it via the GitHub Actions workflow or locally

## Usage (GitHub Actions)

### Search for guides

1. Go to **Actions** > **Search GameFAQ** > **Run workflow**
2. Enter a game title (e.g. `final fantasy vii`)
3. Optionally set a platform filter (e.g. `ps1`)
4. The workflow summary will list matching games and their available FAQs with ratings

### Download a guide

1. Go to **Actions** > **Download GameFAQ** > **Run workflow**
2. Paste a GameFAQ URL from the search results
3. The guide will be downloaded as `.md`, committed, and pushed to the `guides/` folder

You can use either a direct FAQ URL (with `/faqs/` in the path) or a game page URL — the script will auto-find the top-rated guide.

Requires `FIRECRAWL_API_KEY` and `SCRAPINGBEE_API_KEY` repository secrets.

## Local Usage

```bash
pip install -r requirements.txt
python -m playwright install chromium

# Search for a game
python search_faq.py "chrono trigger"

# Download a guide (requires API keys)
export FIRECRAWL_API_KEY="your-key"
python download_faq.py https://gamefaqs.gamespot.com/ps1/57080-chrono-trigger
python download_faq.py https://gamefaqs.gamespot.com/ps1/57080-chrono-trigger/faqs/46950
```

### Download CLI options

- `--firecrawl KEY` — Firecrawl API key (primary method)
- `-s` / `--scrapingbee KEY` — ScrapingBee API key (fallback)
- `-o` / `--output DIR` — output directory (default: `guides/`)

## Search Filters

- `--console` / `-c` — filter by platform (snes, ps1, gba, ds, etc.)
- `-g N` — show FAQ guides for search result #N
- `-a` / `--all-guides` — fetch FAQ listings for all results
- `--markdown` — output as markdown (for CI job summaries)
- `-l` — list results only
