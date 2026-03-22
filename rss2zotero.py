#!/usr/bin/env python3
"""
rss2zotero
----------
Fetches RSS/Atom feeds, deduplicates against previous runs, uses Claude to
filter and summarise new items, pushes hits to a Zotero group library via
the Zotero Web API, and writes a dated Markdown report + log to the output
directory (default: ./-f/).

Usage:
    python rss2zotero.py [--config config.yaml] [--dry-run] [--show-new]

Environment variables (see .env.example):
    ANTHROPIC_API_KEY   — required
    ZOTERO_API_KEY      — required for Zotero push
    SMTP_PASSWORD       — required if email delivery is enabled
"""

import argparse
import hashlib
import json
import logging
import os
import smtplib
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import anthropic
import yaml
from dotenv import load_dotenv

load_dotenv()

# Logging is configured after the output directory is known (see setup_logging).
log = logging.getLogger(__name__)


def setup_logging(output_dir: Path, log_filename: str) -> None:
    """Configure console + rotating file logging into the output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / log_filename
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt, handlers=handlers)
    log.info("Logging to %s", log_path)


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


def item_id(entry: dict) -> str:
    """Stable identifier for a feed entry."""
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Feed fetching & filtering
# ---------------------------------------------------------------------------

def fetch_feed(feed_cfg: dict) -> list[dict]:
    """Fetch one RSS/Atom feed and return a list of item dicts."""
    url = feed_cfg["url"]
    name = feed_cfg["name"]
    log.info("Fetching: %s", name)

    req = urllib.request.Request(url, headers={"User-Agent": "AIXworkbench-Monitor/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()

    root = ET.fromstring(raw)
    ns_atom = "http://www.w3.org/2005/Atom"

    # Detect format: Atom vs RSS 2.0
    if root.tag == f"{{{ns_atom}}}feed" or root.tag == "feed":
        # Atom
        entries = root.findall(f"{{{ns_atom}}}entry")
        def _text(el, tag):
            child = el.find(f"{{{ns_atom}}}{tag}")
            return (child.text or "").strip() if child is not None else ""
        def _link(el):
            for link in el.findall(f"{{{ns_atom}}}link"):
                if link.get("rel", "alternate") == "alternate":
                    return link.get("href", "")
            # fallback: first link element
            link = el.find(f"{{{ns_atom}}}link")
            return link.get("href", "") if link is not None else ""
        raw_entries = [
            {
                "id": _text(e, "id") or _link(e),
                "title": _text(e, "title") or "(no title)",
                "link": _link(e),
                "summary": _text(e, "summary") or _text(e, "content"),
                "published": _text(e, "published") or _text(e, "updated"),
            }
            for e in entries
        ]
    else:
        # RSS 2.0 — root is <rss>, channel is first child
        channel = root.find("channel") or root
        entries = channel.findall("item")
        def _rtext(el, tag):
            child = el.find(tag)
            return (child.text or "").strip() if child is not None else ""
        raw_entries = [
            {
                "id": _rtext(e, "guid") or _rtext(e, "link"),
                "title": _rtext(e, "title") or "(no title)",
                "link": _rtext(e, "link"),
                "summary": _rtext(e, "description"),
                "published": _rtext(e, "pubDate"),
            }
            for e in entries
        ]

    items = [
        {
            "id": item_id(e),
            "source": name,
            "title": e["title"],
            "link": e["link"],
            "summary": e["summary"],
            "published": e["published"],
        }
        for e in raw_entries
    ]
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
    "You are a concise research assistant. Your role is to read a set of RSS feed items "
    "and produce a brief, accurate digest of the most noteworthy ones. "
    "Your audience wants to stay current on their topic without being overwhelmed. "
    "Be specific, cite sources, and never invent details not present in the provided text."
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

    if dry_run:
        log.info("[dry-run] Would send %d items to Claude.", len(items))
        return "[dry-run] Claude summarisation skipped."

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


def write_report(report: str, items: list[dict], config: dict) -> Path:
    """Write the report and a raw-items sidecar JSON to the output directory."""
    today = date.today().isoformat()
    filename = f"ai_literacy_{today}.md"
    items_filename = f"ai_literacy_{today}_items.json"

    primary = Path(config["output"]["directory"])
    fallback = Path(config["output"]["fallback_directory"])

    for directory in (primary, fallback):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            out_path = directory / filename
            out_path.write_text(report, encoding="utf-8")
            log.info("Report written: %s", out_path)
            items_path = directory / items_filename
            items_path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
            log.info("Items sidecar written: %s", items_path)
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
# Zotero group push
# ---------------------------------------------------------------------------

ZOTERO_BATCH_SIZE = 50  # Zotero API max items per write request


def _make_zotero_item(item: dict, cfg: dict) -> dict:
    """Convert a feed item dict to a Zotero API item object."""
    return {
        "itemType": cfg.get("item_type", "webpage"),
        "title": item["title"],
        "url": item["link"],
        "abstractNote": item["summary"][:2000],
        "date": item.get("published", ""),
        "accessDate": date.today().isoformat(),
        "tags": [
            {"tag": cfg.get("tag", "ai-literacy-monitor")},
            {"tag": item.get("source", "")},
        ],
        "extra": f"Source feed: {item.get('source', '')}",
    }


def push_to_zotero(items: list[dict], config: dict, dry_run: bool = False) -> int:
    """Push new feed items to the configured Zotero group library.

    Returns the number of items successfully written (0 on dry-run or error).
    """
    zot_cfg = config.get("zotero", {})
    group_id = str(zot_cfg.get("group_id", "")).strip()
    api_base = zot_cfg.get("api_base", "https://api.zotero.org")

    if not group_id or group_id == "YOUR_ZOTERO_GROUP_ID":
        log.warning("Zotero group_id not configured — skipping Zotero push.")
        return 0

    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    if not api_key:
        log.warning("ZOTERO_API_KEY not set — skipping Zotero push.")
        return 0

    if dry_run:
        log.info("[dry-run] Would push %d item(s) to Zotero group %s.", len(items), group_id)
        return 0

    endpoint = f"{api_base}/groups/{group_id}/items"
    headers = {
        "Zotero-API-Key": api_key,
        "Zotero-API-Version": "3",
        "Content-Type": "application/json",
    }

    zotero_items = [_make_zotero_item(it, zot_cfg) for it in items]
    total_written = 0

    for i in range(0, len(zotero_items), ZOTERO_BATCH_SIZE):
        batch = zotero_items[i: i + ZOTERO_BATCH_SIZE]
        body = json.dumps(batch).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                written = len(result.get("success", {}))
                failed = len(result.get("failed", {}))
                total_written += written
                log.info(
                    "Zotero batch %d–%d: %d written, %d failed.",
                    i + 1, i + len(batch), written, failed,
                )
                if failed:
                    for key, err in result.get("failed", {}).items():
                        log.warning("  Zotero item %s failed: %s", key, err)
        except Exception as exc:
            log.error("Zotero push failed for batch starting at %d: %s", i, exc)

    log.info("Zotero push complete: %d item(s) added to group %s.", total_written, group_id)
    return total_written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Literacy daily digest monitor.")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to config.yaml (default: config.yaml alongside rss2zotero.py)",
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

    # --- Set up output directory and logging ---
    config_dir = Path(args.config).resolve().parent
    out_dir = config_dir / config["output"]["directory"]
    log_filename = config["output"].get("log_file", "monitor.log")
    setup_logging(out_dir, log_filename)

    # --- Load deduplication state ---
    # Resolve relative paths from the config file's directory so state is
    # stored next to the script regardless of the working directory.
    raw_state_path = config["state"]["seen_file"]
    state_path = str(config_dir / raw_state_path)
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
        write_report(report, new_items, config)
    else:
        log.info("[dry-run] Report preview:\n%s", report)

    # --- Push to Zotero ---
    push_to_zotero(new_items, config, dry_run=args.dry_run)

    # --- Email ---
    if "email" in config:
        send_email(report, config, dry_run=args.dry_run)

    # --- Persist seen state ---
    today_str = datetime.now(timezone.utc).isoformat()
    for item in all_items:
        seen.setdefault(item["id"], today_str)
    save_seen(state_path, seen)
    log.info("State saved. Done.")


if __name__ == "__main__":
    main()
