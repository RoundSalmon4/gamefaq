# gamefaq

Search and download GameFAQs guides as markdown.

GameFAQs blocks VPN users and has Cloudflare protections, so this project uses Firecrawl to bypass those restrictions in CI. Both search and downloads go through Firecrawl's hosted infrastructure, which works from any network without being IP-blocked.

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

Requires a `FIRECRAWL_API_KEY` repository secret.

## Local Usage

```bash
pip install -r requirements.txt

# Search for a game (requires a Firecrawl API key)
export FIRECRAWL_API_KEY="your-key"
python search_faq.py "chrono trigger"
python search_faq.py "monster hunter rise" -c switch

# Download a guide (requires API keys)
python download_faq.py https://gamefaqs.gamespot.com/ps1/57080-chrono-trigger
python download_faq.py https://gamefaqs.gamespot.com/ps1/57080-chrono-trigger/faqs/46950
```

### Download CLI options

- `--firecrawl KEY` — Firecrawl API key
- `-o` / `--output DIR` — output directory (default: `guides/`)
- `--commit-title FILE` — write a commit title (e.g. `Add <game> guide`) to FILE

## Search CLI options

- `--firecrawl KEY` — Firecrawl API key (required for search)
- `-c` / `--console PLATFORM` — filter by platform (snes, ps1, gba, ds, etc.)
- `-g N` — show FAQ guides for search result #N
- `-a` / `--all-guides` — fetch FAQ listings for all results
- `--min-relevance FLOAT` — minimum relevance threshold (default 0.3)
- `--markdown` — output as markdown (for CI job summaries)
- `-l` — list results only
