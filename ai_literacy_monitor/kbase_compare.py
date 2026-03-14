#!/usr/bin/env python3
"""
kbase_compare.py — Compare a monitor report's raw items against a kbase document.

For each article that passed the keyword filter on a given run, asks Claude to:
  1. Identify which aspects of the kbase it relates to (by section/concept).
  2. Note whether it confirms, extends, complicates, or contradicts the kbase thinking.
  3. Flag anything genuinely novel that the kbase doesn't address.

Outputs a dated Markdown comparison report.

Usage:
    python kbase_compare.py \\
        --items   reports/ai_literacy_2026-03-14_items.json \\
        --kbase   kbase/operational_synthesis_jan_feb_2026.md \\
        [--config config.yaml] \\
        [--dry-run]

The items JSON is the sidecar file written by monitor.py alongside each report.
If no --items path is given, the script will attempt to fetch the feeds live
(same behaviour as monitor.py --show-new) and use all keyword-matching items.
"""

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import anthropic
import yaml
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a research assistant supporting an AI literacy curriculum development "
    "project at SUNY Polytechnic Institute. You have deep familiarity with the project's "
    "theoretical framework: AI literacy as reading, thinking, and writing (RTW) using the "
    "tools, techniques, and technological systems consistent with one's peers; the MICA "
    "methodology; the fluency/literacy distinction; and the mandate context (SUNY Board "
    "Resolution 2024-64, Fall 2026 system-wide implementation). "
    "Be direct and practical. The audience is the faculty member building this program."
)

COMPARE_PROMPT_TEMPLATE = """\
Below is the project's current operational knowledge base, followed by {n} recent news/research items.

For EACH item, produce a brief comparison entry with this structure:

**[{item_number_placeholder}] {title_placeholder}**
- **Kbase relevance**: Which section(s) or concept(s) does this connect to?
- **Relationship**: Does this item *confirm*, *extend*, *complicate*, or *contradict* the kbase thinking? One sentence.
- **Actionable note**: One concrete sentence on whether/how this should influence ongoing work (or "No action needed" if it doesn't).

After all items, add a short section:

## Overall Patterns
2–4 bullet observations about what this batch of articles reveals collectively, relative to the kbase.

---
KBASE:
{kbase}

---
ITEMS:
{items}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def load_kbase(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def load_items_from_json(path: str) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fetch_live_items(config: dict) -> list[dict]:
    """Re-fetch feeds live and return keyword-matching items (ignores seen state)."""
    # Import helpers from monitor.py sitting in the same directory
    sys.path.insert(0, str(Path(__file__).parent))
    from monitor import fetch_all_feeds, filter_new

    all_items = fetch_all_feeds(config)
    keywords = config["claude"].get("filter_keywords", [])
    # Pass empty seen dict so nothing is filtered by deduplication
    return filter_new(all_items, {}, keywords)


def format_items(items: list[dict]) -> str:
    parts = []
    for i, item in enumerate(items, 1):
        summary = item.get("summary", "")[:600].replace("\n", " ").strip()
        parts.append(
            f"[{i}] SOURCE: {item['source']}\n"
            f"    TITLE: {item['title']}\n"
            f"    URL: {item['link']}\n"
            f"    SUMMARY: {summary}"
        )
    return "\n\n".join(parts)


def truncate_kbase(kbase: str, max_chars: int = 12000) -> str:
    """Truncate kbase to fit comfortably in the prompt."""
    if len(kbase) <= max_chars:
        return kbase
    log.warning("Kbase truncated from %d to %d chars for prompt.", len(kbase), max_chars)
    return kbase[:max_chars] + "\n\n[... truncated for length ...]"


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

def run_comparison(items: list[dict], kbase: str, config: dict, dry_run: bool) -> str:
    if dry_run:
        log.info("[dry-run] Would compare %d items against kbase.", len(items))
        return "[dry-run] Comparison skipped."

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY is not set.")
        sys.exit(1)

    prompt = COMPARE_PROMPT_TEMPLATE.format(
        n=len(items),
        item_number_placeholder="N",
        title_placeholder="TITLE",
        kbase=truncate_kbase(kbase),
        items=format_items(items),
    )
    # Fix the placeholder text that was used as format() keys
    # (they were intentionally non-numeric so format() ignored them)

    model = config["claude"]["model"]
    log.info("Sending %d items to Claude (%s) for kbase comparison…", len(items), model)
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

REPORT_TEMPLATE = """\
# AI Literacy Kbase Comparison — {date}

> Generated by AIXworkbench kbase_compare · {timestamp} UTC
> Kbase: `{kbase_path}`
> Items source: `{items_source}`

{comparison}

---
*{n} items compared against kbase.*
"""


def write_comparison_report(
    comparison: str,
    n: int,
    kbase_path: str,
    items_source: str,
    config: dict,
) -> Path:
    today = date.today().isoformat()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    report = REPORT_TEMPLATE.format(
        date=today,
        timestamp=ts,
        kbase_path=kbase_path,
        items_source=items_source,
        comparison=comparison,
        n=n,
    )

    filename = f"ai_literacy_{today}_kbase_compare.md"
    primary = Path(config["output"]["directory"])
    fallback = Path(config["output"]["fallback_directory"])

    for directory in (primary, fallback):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            out_path = directory / filename
            out_path.write_text(report, encoding="utf-8")
            log.info("Comparison report written: %s", out_path)
            return out_path
        except OSError as exc:
            log.warning("Cannot write to %s: %s — trying fallback.", directory, exc)

    raise RuntimeError("Could not write comparison report to any configured directory.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare monitor items against an AI literacy kbase document."
    )
    parser.add_argument(
        "--items",
        help="Path to a *_items.json sidecar from a previous monitor run. "
             "If omitted, feeds are fetched live.",
    )
    parser.add_argument(
        "--kbase",
        default=str(Path(__file__).parent / "kbase" / "operational_synthesis_jan_feb_2026.md"),
        help="Path to the kbase Markdown file.",
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to config.yaml.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    kbase = load_kbase(args.kbase)
    log.info("Kbase loaded: %s (%d chars)", args.kbase, len(kbase))

    if args.items:
        items = load_items_from_json(args.items)
        items_source = args.items
        log.info("Loaded %d items from %s", len(items), args.items)
    else:
        log.info("No --items file given; fetching feeds live…")
        items = fetch_live_items(config)
        items_source = "live fetch"
        log.info("Fetched %d keyword-matching items live.", len(items))

    if not items:
        log.info("No items to compare.")
        return

    comparison = run_comparison(items, kbase, config, dry_run=args.dry_run)

    if args.dry_run:
        print(comparison)
        return

    write_comparison_report(
        comparison=comparison,
        n=len(items),
        kbase_path=args.kbase,
        items_source=items_source,
        config=config,
    )


if __name__ == "__main__":
    main()
