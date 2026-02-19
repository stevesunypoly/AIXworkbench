#!/usr/bin/env python3
"""
AI Literacy Monitor
-------------------
Fetches RSS feeds, deduplicates against previous runs, asks Claude to
summarize new developments in AI literacy, and delivers a dated Markdown
report to a SharePoint folder and via email.

Usage:
    python monitor.py [--config path/to/config.yaml] [--dry-run]

Environment variables (see .env.example):
    ANTHROPIC_API_KEY   — required
    SMTP_PASSWORD       — required if email is enabled
"""

import argparse
import hashlib
import json
import logging
import os
import smtplib
import sys
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import anthropic
import feedparser
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
# Config
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# State (seen-item deduplication)
# ---------------------------------------------------------------------------

def load_seen(state_path: str) -> dict:
    """Return {item_id: iso_date_str} mapping of previously seen items."""
    p = Path(state_path)
    if not p.exists():
        return {}
    with open(p) as fh:
        return json.load(fh)


def save_seen(state_path: str, seen: dict) -> None:
    p = Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as fh:
        json.dump(seen, fh, indent=2)


def prune_seen(seen: dict, max_age_days: int) -> dict:
    """Drop entries older than max_age_days to keep the state file small."""
    cutoff = datetime.now(timezone.utc).toordinal() - max_age_days
    return {
        k: v
        for k, v in seen.items()
        if datetime.fromisoformat(v).toordinal() >= cutoff
    }


def item_id(entry: feedparser.FeedParserDict) -> str:
    """Stable identifier for a feed entry."""
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Feed fetching & filtering
# ---------------------------------------------------------------------------

def fetch_feed(feed_cfg: dict) -> list[dict]:
    """Fetch one RSS feed and return a list of item dicts."""
    url = feed_cfg["url"]
    name = feed_cfg["name"]
    log.info("Fetching: %s", name)
    parsed = feedparser.parse(url)
    if parsed.bozo:
        log.warning("Feed parse issue for '%s': %s", name, parsed.bozo_exception)
    items = []
    for entry in parsed.entries:
        items.append(
            {
                "id": item_id(entry),
                "source": name,
                "title": entry.get("title", "(no title)"),
                "link": entry.get("link", ""),
                "summary": entry.get("summary", entry.get("description", "")),
                "published": entry.get("published", ""),
            }
        )
    log.info("  → %d items", len(items))
    return items


def keyword_match(item: dict, keywords: list[str]) -> bool:
    """Return True if title or summary contains at least one keyword (case-insensitive)."""
    haystack = (item["title"] + " " + item["summary"]).lower()
    return any(kw.lower() in haystack for kw in keywords)


def fetch_all_feeds(config: dict) -> list[dict]:
    all_items: list[dict] = []
    for feed_cfg in config["feeds"]:
        try:
            all_items.extend(fetch_feed(feed_cfg))
        except Exception as exc:
            log.error("Failed to fetch '%s': %s", feed_cfg["name"], exc)
    return all_items


def filter_new(
    items: list[dict],
    seen: dict,
    keywords: list[str],
) -> list[dict]:
    """Return items that are new and pass the keyword filter."""
    new_items = []
    for item in items:
        if item["id"] in seen:
            continue
        if keywords and not keyword_match(item, keywords):
            continue
        new_items.append(item)
    return new_items


# ---------------------------------------------------------------------------
# Claude summarisation
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a concise academic research assistant specialising in AI literacy, "
    "pedagogy, and the ethical/material dimensions of AI in higher education. "
    "Your audience is a faculty member who wants to stay current without being overwhelmed."
)

USER_PROMPT_TEMPLATE = """\
Below are {n} new items gathered from academic and higher-education news sources today.
Produce a digest of the most noteworthy developments. Follow these rules exactly:
- Output bullet points ONLY — no headers, no preamble, no conclusion.
- Limit to {max} bullets (fewer is fine if content doesn't warrant more; aim for at least {min}).
- Each bullet: one sentence summary + parenthetical source attribution, e.g.:
  • Universities are piloting AI literacy frameworks that foreground ethical reasoning alongside tool skills. (Inside Higher Ed)
- Prioritise: novel research findings > policy/institutional announcements > practitioner commentary.
- Omit items that are purely commercial/promotional.
- Do NOT invent details not present in the provided text.

ITEMS:
{items}
"""


def format_items_for_prompt(items: list[dict]) -> str:
    parts = []
    for i, item in enumerate(items, 1):
        # Truncate long summaries to keep the prompt manageable
        summary = item["summary"][:500].replace("\n", " ").strip()
        parts.append(
            f"[{i}] SOURCE: {item['source']}\n"
            f"    TITLE: {item['title']}\n"
            f"    SUMMARY: {summary}\n"
            f"    URL: {item['link']}"
        )
    return "\n\n".join(parts)


def summarise_with_claude(
    items: list[dict],
    config: dict,
    dry_run: bool = False,
) -> Optional[str]:
    """Ask Claude to produce a bullet-point digest. Returns the digest string."""
    if not items:
        log.info("No new items to summarise.")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY is not set.")
        sys.exit(1)

    max_b = config["report"]["max_bullets"]
    min_b = config["report"]["min_bullets"]
    prompt = USER_PROMPT_TEMPLATE.format(
        n=len(items),
        max=max_b,
        min=min_b,
        items=format_items_for_prompt(items),
    )

    if dry_run:
        log.info("[dry-run] Would send %d items to Claude.", len(items))
        return "[dry-run] Claude summarisation skipped."

    log.info("Sending %d items to Claude (%s)…", len(items), config["claude"]["model"])
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=config["claude"]["model"],
        max_tokens=config["claude"]["max_tokens"],
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

REPORT_TEMPLATE = """\
# AI Literacy Digest — {date}

> Generated by AIXworkbench AI Literacy Monitor · {timestamp} UTC

{bullets}

---
*{item_count} new item(s) processed from {feed_count} source(s).*
"""


def build_report(digest: str, item_count: int, feed_count: int) -> str:
    today = date.today().isoformat()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return REPORT_TEMPLATE.format(
        date=today,
        timestamp=ts,
        bullets=digest,
        item_count=item_count,
        feed_count=feed_count,
    )


def write_report(report: str, config: dict) -> Path:
    """Write the report to the configured output directory. Falls back to local dir."""
    today = date.today().isoformat()
    filename = f"ai_literacy_{today}.md"

    primary = Path(config["output"]["directory"])
    fallback = Path(config["output"]["fallback_directory"])

    for directory in (primary, fallback):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            out_path = directory / filename
            out_path.write_text(report, encoding="utf-8")
            log.info("Report written: %s", out_path)
            return out_path
        except OSError as exc:
            log.warning("Cannot write to %s: %s — trying fallback.", directory, exc)

    raise RuntimeError("Could not write report to any configured directory.")


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def send_email(report: str, config: dict, dry_run: bool = False) -> None:
    email_cfg = config["email"]
    today = date.today().isoformat()
    subject = f"{email_cfg['subject_prefix']} — {today}"

    if dry_run:
        log.info("[dry-run] Would email '%s' to %s.", subject, email_cfg["to"])
        return

    password = os.environ.get("SMTP_PASSWORD")
    if not password:
        log.warning("SMTP_PASSWORD not set — skipping email delivery.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg["from"]
    msg["To"] = email_cfg["to"]

    # Plain-text part (strip Markdown emphasis markers for readability)
    plain = report.replace("**", "").replace("*", "").replace("`", "")
    msg.attach(MIMEText(plain, "plain", "utf-8"))

    try:
        with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
            if email_cfg.get("use_tls", True):
                server.starttls()
            server.login(email_cfg["from"], password)
            server.sendmail(email_cfg["from"], email_cfg["to"], msg.as_string())
        log.info("Email sent to %s.", email_cfg["to"])
    except Exception as exc:
        log.error("Email delivery failed: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Literacy daily digest monitor.")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to config.yaml (default: config.yaml next to this script)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch feeds and show what would be sent, but skip Claude API and email.",
    )
    parser.add_argument(
        "--show-new",
        action="store_true",
        help="Print titles of new items before summarisation.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    # --- Load deduplication state ---
    state_path = config["state"]["seen_file"]
    seen = load_seen(state_path)
    seen = prune_seen(seen, config["state"]["max_age_days"])
    log.info("Loaded %d previously seen item IDs.", len(seen))

    # --- Fetch & filter ---
    all_items = fetch_all_feeds(config)
    log.info("Total items fetched across all feeds: %d", len(all_items))

    keywords = config["claude"].get("filter_keywords", [])
    new_items = filter_new(all_items, seen, keywords)
    log.info("New items after deduplication + keyword filter: %d", len(new_items))

    if args.show_new:
        for item in new_items:
            print(f"  [{item['source']}] {item['title']}")

    if not new_items:
        log.info("Nothing new today — no report generated.")
        # Still update seen so re-fetched old items stay suppressed
        today_str = datetime.now(timezone.utc).isoformat()
        for item in all_items:
            seen.setdefault(item["id"], today_str)
        save_seen(state_path, seen)
        return

    # --- Summarise ---
    digest = summarise_with_claude(new_items, config, dry_run=args.dry_run)
    if not digest:
        log.error("No digest produced.")
        return

    # --- Build & write report ---
    report = build_report(
        digest=digest,
        item_count=len(new_items),
        feed_count=len(config["feeds"]),
    )

    if not args.dry_run:
        write_report(report, config)
    else:
        log.info("[dry-run] Report preview:\n%s", report)

    # --- Email ---
    send_email(report, config, dry_run=args.dry_run)

    # --- Persist seen state ---
    today_str = datetime.now(timezone.utc).isoformat()
    for item in all_items:
        seen.setdefault(item["id"], today_str)
    save_seen(state_path, seen)
    log.info("State saved. Done.")


if __name__ == "__main__":
    main()
