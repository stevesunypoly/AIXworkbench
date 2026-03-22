# rss2zotero

**Python script with YAML config to harness RSS feeds and upload new hits to a Zotero group library via the Zotero Web API.**

Configure any set of RSS/Atom feeds and a list of filter keywords. On each run, `rss2zotero` fetches all feeds, deduplicates against previous runs, passes new items through Claude for keyword filtering and summarisation, and uploads matching items directly to your Zotero group. A dated Markdown digest and a full run log are written to the output directory.

---

## How it works

```
RSS/Atom feeds
      │
      ▼
  fetch + deduplicate          ← seen_items.json tracks what's already been processed
      │
      ▼
  keyword pre-filter           ← fast local check before hitting the API
      │
      ▼
  Claude summarisation         ← bullet-point digest of new items
      │
      ├──▶  Markdown report    → ./-f/rss2zotero_YYYY-MM-DD.md
      ├──▶  Raw items JSON     → ./-f/rss2zotero_YYYY-MM-DD_items.json
      ├──▶  Run log            → ./-f/rss2zotero.log
      └──▶  Zotero group push  → Zotero Web API v3
```

---

## Requirements

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com)
- A [Zotero account](https://www.zotero.org) with a group library
- A Zotero API key with **Write** access to that group

---

## Installation

```bash
git clone https://github.com/YOUR-USERNAME/rss2zotero.git
cd rss2zotero
pip install -r requirements.txt
```

---

## Configuration

### 1. Copy the example files

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

### 2. Fill in `.env`

```env
ANTHROPIC_API_KEY=sk-ant-...
ZOTERO_API_KEY=your_zotero_api_key_here
```

Get your Zotero API key at **https://www.zotero.org/settings/keys/new** — grant *Write* access to your group library.

### 3. Edit `config.yaml`

**Add your feeds:**
```yaml
feeds:
  - name: "My journal feed"
    url: "https://example.com/rss.xml"
    category: "journal"
```

Any RSS 2.0 or Atom feed works. Most WordPress sites, journals (Nature, Wiley, Frontiers), and news outlets publish one.

**Set your Zotero group ID** — the number in the URL at `https://www.zotero.org/groups/<id>/`:
```yaml
zotero:
  group_id: "1234567"
```

**Set your keyword filter** — items must match at least one keyword to be processed:
```yaml
claude:
  filter_keywords:
    - "machine learning"
    - "climate"
    - "your topic"
```

Leave `filter_keywords` empty (`[]`) to pass all feed items through.

---

## Usage

```bash
# Safe preview — fetches feeds but skips Claude and Zotero
python rss2zotero.py --dry-run --show-new

# Live run
python rss2zotero.py

# Custom config file
python rss2zotero.py --config /path/to/my-config.yaml
```

### Output

All files land in `./-f/` (configurable in `config.yaml`):

```
-f/
  rss2zotero_2026-03-22.md          # Claude-generated digest
  rss2zotero_2026-03-22_items.json  # raw matched items
  rss2zotero.log                    # full run log
  seen_items.json                   # deduplication state
```

---

## Running on a schedule

### macOS / Linux — cron

```cron
# Daily at 07:00
0 7 * * * cd /path/to/rss2zotero && python rss2zotero.py >> /path/to/rss2zotero/-f/cron.log 2>&1
```

### GitHub Actions

Create `.github/workflows/daily.yml`:

```yaml
name: rss2zotero daily run

on:
  schedule:
    - cron: "0 7 * * *"   # 07:00 UTC daily
  workflow_dispatch:        # allow manual trigger

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: python rss2zotero.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          ZOTERO_API_KEY: ${{ secrets.ZOTERO_API_KEY }}
      - uses: actions/upload-artifact@v4
        with:
          name: digest-${{ github.run_id }}
          path: ./-f/*.md
```

Add `ANTHROPIC_API_KEY` and `ZOTERO_API_KEY` as repository secrets under **Settings → Secrets and variables → Actions**.

---

## Feed ideas

Any RSS/Atom URL works. A few starting points by domain:

| Source | RSS URL pattern |
|---|---|
| Google Alerts | Set delivery to "RSS feed" at google.com/alerts |
| WordPress blogs/labs | `https://example.com/feed/` |
| Nature journals | `https://www.nature.com/rss/feeds/<section>.rss` |
| Wiley journals | `https://onlinelibrary.wiley.com/feed/<issn>/most-recent` |
| Frontiers | `https://www.frontiersin.org/journals/<journal>/rss` |
| University World News Africa | `https://www.universityworldnews.com/rss.php?edition=africa` |
| Stanford HAI | `https://hai.stanford.edu/news/rss.xml` |

---

## Project layout

```
rss2zotero.py          main script
config.example.yaml    annotated config template
.env.example           environment variable template
requirements.txt       Python dependencies
LICENSE                BSD 3-Clause
```

---

## License

BSD 3-Clause — © 2025 Steve Schneider. See [LICENSE](LICENSE).
