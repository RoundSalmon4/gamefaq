# gamefaq

Search and download GameFAQs guides as plain text.

GameFAQs blocks VPN users and has Cloudflare protections, so this project uses Playwright to bypass those restrictions both locally and in CI.

## Workflow

1. **Search** for a game and browse available guides
2. **Copy** the guide URL you want
3. **Download** it via the GitHub Actions workflow or locally

## Usage (GitHub Actions)

### Search for guides

1. Go to **Actions** > **Search GameFAQ** > **Run workflow**
2. Enter a game title (e.g. `final fantasy vii`)
3. Optionally set a platform filter (e.g. `ps1`)
4. The workflow summary will list matching games and their available FAQs with ratings

### Download a guide

1. Go to **Actions** > **Download GameFAQ** > **Run workflow**
2. Paste the GameFAQ URL from the search results
3. The guide will be downloaded, committed, and pushed to the `guides/` folder

## Local Usage

```bash
pip install -r requirements.txt
python -m playwright install chromium

# Search for a game
python search_faq.py "chrono trigger"

# Download a guide
python download_faq.py <url>
```

## Search Filters

- `--console` / `-c` — filter by platform (snes, ps1, gba, ds, etc.)
- `-g N` — show FAQ guides for search result #N
- `-a` / `--all-guides` — fetch FAQ listings for all results
- `--markdown` — output as markdown (for CI job summaries)
- `-l` — list results only
