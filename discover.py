"""
discover.py — Auto-discover and validate public XMLTV EPG sources.

Scans known EPG registries/repositories for sources that contain US channel
data, probes them for validity, and maintains a discovered_sources.json file
that the fetcher merges in automatically.

Usage:
  python discover.py                        # uses config.json
  python discover.py --config config.json   # explicit config
"""

import gzip
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import requests

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DISCOVERED_PATH = os.path.join(BASE_DIR, "discovered_sources.json")
USER_AGENT = "EPG-Aggregator/1.0 (auto-discovery)"
PROBE_TIMEOUT = 30  # seconds per URL
# Minimum US channels a source must have to be worth adding
MIN_US_CHANNELS = 5
# Max age in hours before a source is considered stale
STALE_HOURS = 48


# ---------------------------------------------------------------------------
# Known EPG registries / search patterns
# ---------------------------------------------------------------------------

# Direct URLs to try — curated list of known public XMLTV sources
KNOWN_URLS: list[dict] = [
    # iptv-org community EPG (very large, well-maintained)
    {"url": "https://iptv-org.github.io/epg/guides/us/tvtv.us.xml", "name": "iptv-org - TVTV US"},
    {"url": "https://iptv-org.github.io/epg/guides/us/directv.com.xml", "name": "iptv-org - DirecTV US"},
    {"url": "https://iptv-org.github.io/epg/guides/us/gatewayblend.com.xml", "name": "iptv-org - GatewayBlend US"},
    {"url": "https://iptv-org.github.io/epg/guides/us/ontvtonight.com.xml", "name": "iptv-org - OnTVTonight US"},
    # Open-EPG additional files
    {"url": "https://www.open-epg.com/files/unitedstates1.xml", "name": "Open-EPG - US (1)"},
    {"url": "https://www.open-epg.com/files/unitedstates2.xml", "name": "Open-EPG - US (2)"},
    {"url": "https://www.open-epg.com/files/unitedstates4.xml", "name": "Open-EPG - US (4)"},
    {"url": "https://www.open-epg.com/files/unitedstates5.xml", "name": "Open-EPG - US (5)"},
    {"url": "https://www.open-epg.com/files/unitedstates6.xml", "name": "Open-EPG - US (6)"},
    {"url": "https://www.open-epg.com/files/unitedstates7.xml", "name": "Open-EPG - US (7)"},
    {"url": "https://www.open-epg.com/files/unitedstates8.xml", "name": "Open-EPG - US (8)"},
    {"url": "https://www.open-epg.com/files/unitedstates9.xml", "name": "Open-EPG - US (9)"},
    {"url": "https://www.open-epg.com/files/unitedstates12.xml", "name": "Open-EPG - US (12)"},
    {"url": "https://www.open-epg.com/files/unitedstates13.xml", "name": "Open-EPG - US (13)"},
    # EPGShare01 additional
    {"url": "https://epgshare01.online/epgshare01/epg_ripper_US1.xml.gz", "name": "EPGShare01 - US Alt"},
    {"url": "https://epgshare01.online/epgshare01/epg_ripper_US3.xml.gz", "name": "EPGShare01 - US 3"},
    # IPTV-EPG.org variants
    {"url": "https://iptv-epg.org/files/epg-us-en.xml", "name": "IPTV-EPG.org - US English"},
    # i.mjh.nz (Melbourne Internet Hub — very reliable, updated frequently)
    {"url": "https://i.mjh.nz/PlutoTV/us.xml.gz", "name": "MJH - PlutoTV US"},
    {"url": "https://i.mjh.nz/SamsungTVPlus/us.xml.gz", "name": "MJH - Samsung TV Plus US"},
    {"url": "https://i.mjh.nz/Plex/us.xml.gz", "name": "MJH - Plex US"},
    {"url": "https://i.mjh.nz/Stirr/all.xml.gz", "name": "MJH - Stirr US"},
]

# GitHub repos that host EPG files — we'll scan their file listings
GITHUB_REPOS: list[dict] = [
    {
        "api": "https://api.github.com/repos/iptv-org/epg/contents/guides/us",
        "base_url": "https://iptv-org.github.io/epg/guides/us/",
        "prefix": "iptv-org - ",
    },
]


def _fetch_bytes(url: str, max_bytes: int = 512_000) -> bytes | None:
    """Fetch up to max_bytes from a URL. Returns None on failure."""
    try:
        resp = requests.get(
            url, timeout=PROBE_TIMEOUT, stream=True,
            headers={"User-Agent": USER_AGENT}
        )
        resp.raise_for_status()
        chunks = []
        total = 0
        for chunk in resp.iter_content(8192):
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
        resp.close()
        return b"".join(chunks)
    except Exception:
        return None


def _probe_source(url: str) -> dict | None:
    """
    Probe a URL to check if it's a valid XMLTV source with US channels.
    Returns a stats dict or None if invalid/unreachable.
    """
    raw = _fetch_bytes(url)
    if raw is None:
        return None

    # Decompress if gzipped
    content = raw
    if url.endswith(".gz"):
        try:
            content = gzip.decompress(raw)
        except Exception:
            return None

    # Try parsing as XML — might be partial so just scan for patterns
    text = content.decode("utf-8", errors="ignore")

    # Quick validity check — must have <tv and <channel
    if "<tv" not in text or "<channel" not in text:
        return None

    # Count channels and US channels
    ch_ids = re.findall(r'<channel\s+id="([^"]*)"', text)
    prog_count = text.count("<programme")

    # Heuristic for US channels: ID contains ".us" or common US patterns
    us_channels = [c for c in ch_ids if ".us" in c.lower() or
                   re.search(r'\b(ESPN|CNN|HBO|FOX|NBC|CBS|ABC|TNT|USA|AMC|TBS)\b', c, re.I)]

    # Check for programme freshness — find a recent start time
    starts = re.findall(r'start="(\d{8})', text)
    freshness = None
    if starts:
        try:
            latest = max(starts)
            latest_dt = datetime.strptime(latest, "%Y%m%d").replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - latest_dt).total_seconds() / 3600
            freshness = age_hours
        except Exception:
            pass

    return {
        "total_channels": len(ch_ids),
        "us_channels": len(us_channels),
        "programmes": prog_count,
        "freshness_hours": freshness,
        "sample_ids": ch_ids[:5],
    }


def _discover_from_github(repo_info: dict) -> list[dict]:
    """Scan a GitHub repo API for XMLTV files."""
    discovered = []
    try:
        resp = requests.get(
            repo_info["api"], timeout=15,
            headers={"User-Agent": USER_AGENT}
        )
        if resp.status_code != 200:
            return []
        files = resp.json()
        for f in files:
            name = f.get("name", "")
            if name.endswith(".xml") or name.endswith(".xml.gz"):
                url = repo_info["base_url"] + name
                display = repo_info["prefix"] + name.replace(".xml.gz", "").replace(".xml", "")
                discovered.append({"url": url, "name": display})
    except Exception as e:
        logger.warning(f"  GitHub scan failed for {repo_info['api']}: {e}")
    return discovered


def load_existing() -> dict:
    """Load the current discovered_sources.json."""
    if os.path.exists(DISCOVERED_PATH):
        with open(DISCOVERED_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"sources": [], "last_scan": None, "scan_stats": {}}


def save_discovered(data: dict):
    """Write discovered_sources.json."""
    with open(DISCOVERED_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def run_discovery(config_path: str = "config.json") -> dict:
    """
    Main discovery entry point.
    1. Gather candidate URLs from known lists + GitHub repos
    2. Filter out URLs already in the user's config
    3. Probe each candidate
    4. Keep sources that meet quality thresholds
    5. Update discovered_sources.json
    """
    # Load user config to avoid duplicating existing sources
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    existing_urls = {s["url"] for s in config.get("sources", [])}
    existing_data = load_existing()
    # Also track previously discovered URLs and their status
    prev_sources = {s["url"]: s for s in existing_data.get("sources", [])}

    # Gather all candidate URLs
    candidates = list(KNOWN_URLS)

    # Scan GitHub repos
    for repo in GITHUB_REPOS:
        logger.info(f"  Scanning GitHub: {repo['api']}")
        candidates.extend(_discover_from_github(repo))

    # Deduplicate and filter out already-configured sources
    seen_urls = set()
    unique = []
    for c in candidates:
        url = c["url"]
        if url in seen_urls or url in existing_urls:
            continue
        seen_urls.add(url)
        unique.append(c)

    logger.info(f"  {len(unique)} candidate URLs to probe (after filtering {len(existing_urls)} existing)")

    # Probe each candidate
    good_sources = []
    probed = 0
    failed = 0
    stale = 0

    for candidate in unique:
        url = candidate["url"]
        name = candidate["name"]
        probed += 1

        logger.info(f"  [{probed}/{len(unique)}] Probing: {name}")
        stats = _probe_source(url)

        if stats is None:
            logger.info(f"    ✗ Unreachable or invalid")
            failed += 1
            # Mark previously good source as failed
            if url in prev_sources:
                prev = prev_sources[url]
                prev["consecutive_failures"] = prev.get("consecutive_failures", 0) + 1
            continue

        us_ch = stats["us_channels"]
        total_ch = stats["total_channels"]
        progs = stats["programmes"]
        fresh = stats["freshness_hours"]

        # Quality gate
        if us_ch < MIN_US_CHANNELS:
            logger.info(f"    ✗ Only {us_ch} US channels (need {MIN_US_CHANNELS}+)")
            continue

        if fresh is not None and fresh > STALE_HOURS:
            logger.info(f"    ✗ Stale data ({fresh:.0f}h old, limit {STALE_HOURS}h)")
            stale += 1
            continue

        fresh_str = f"{fresh:.0f}h old" if fresh is not None else "unknown age"
        logger.info(f"    ✓ {us_ch} US channels, {total_ch} total, {progs} progs, {fresh_str}")

        source_entry = {
            "name": f"[Auto] {name}",
            "url": url,
            "enabled": True,
            "prefer_for": ["*.us"],
            "auto_discovered": True,
            "us_channels": us_ch,
            "total_channels": total_ch,
            "last_probed": datetime.now(timezone.utc).isoformat(),
            "freshness_hours": fresh,
            "consecutive_failures": 0,
        }
        good_sources.append(source_entry)

    # Merge with previously discovered sources:
    # - Keep previously discovered sources that are still good
    # - Disable sources with 3+ consecutive failures
    final = []
    final_urls = {s["url"] for s in good_sources}

    for s in good_sources:
        final.append(s)

    for url, prev in prev_sources.items():
        if url in final_urls or url in existing_urls:
            continue
        failures = prev.get("consecutive_failures", 0)
        if failures >= 3:
            prev["enabled"] = False
            prev["disable_reason"] = f"Failed {failures} consecutive probes"
            logger.info(f"  Disabling {prev['name']}: {failures} consecutive failures")
        final.append(prev)

    # Assign priorities (start at 50 to stay below manual sources)
    for i, s in enumerate(final):
        s["priority"] = 50 + i

    result = {
        "sources": final,
        "last_scan": datetime.now(timezone.utc).isoformat(),
        "scan_stats": {
            "candidates_found": len(unique),
            "probed": probed,
            "passed": len(good_sources),
            "failed": failed,
            "stale": stale,
        },
    }

    save_discovered(result)
    logger.info(
        f"  Discovery complete: {len(good_sources)} new sources, "
        f"{len(final)} total discovered, saved to {DISCOVERED_PATH}"
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Discover public EPG sources")
    parser.add_argument("--config", default="config.json",
                        help="Path to config JSON file (default: config.json)")
    args = parser.parse_args()

    logger.info("Starting EPG source discovery...")
    result = run_discovery(args.config)

    stats = result["scan_stats"]
    print(f"\nDiscovery results:")
    print(f"  Candidates scanned: {stats['candidates_found']}")
    print(f"  Passed quality gate: {stats['passed']}")
    print(f"  Failed/unreachable: {stats['failed']}")
    print(f"  Stale data: {stats['stale']}")
    print(f"  Total discovered sources: {len(result['sources'])}")
    print(f"\nSaved to: {DISCOVERED_PATH}")
