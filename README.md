# gamefaq

Search and download GameFAQs guides as plain text.

GameFAQs blocks VPN users and has Cloudflare protections, so this project uses Playwright to bypass those restrictions both locally and in CI.

## Workflow

1. **Search** for a game and browse available guides
2. **Copy** the guide URL you want
3. **Download** it via the GitHub Actions workflow or locally

## Usage (GitHub Actions)

### Search for guides

```bash
python search_faq.py "final fantasy vii"
python search_faq.py "zelda" --console snes
python search_faq.py "pokemon" -g 1     # show FAQs for result #1
```

### Download a guide

1. Go to **Actions** > **Download GameFAQ** > **Run workflow**
2. Paste the GameFAQ URL (e.g. `https://gamefaqs.gamespot.com/ps/196853-final-fantasy-vii/faqs/57145`)
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
- `-l` — list results only
