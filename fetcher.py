"""
fetcher.py — Downloads, parses, and merges XMLTV EPG sources.

Master-source mode:
  If any source has "master": true in config, that source's channel list
  is used as the single source of truth for channel IDs. All other sources
  auto-match their channels to the master by display name and contribute
  programme data under the master's channel IDs.
"""

import fnmatch
import gzip
import json
import logging
import os
import re
from copy import deepcopy
from datetime import datetime
from xml.etree import ElementTree as ET

import requests

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
MERGED_PATH = os.path.join(CACHE_DIR, "merged.xml")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# Stored fetch result for the status page
last_result: dict = {}


def set_config_path(path: str):
    """Override the config file path and derive cache paths from it.
    This allows multiple instances with different configs (e.g. Starlite vs MyBunny).
    """
    global CONFIG_PATH, CACHE_DIR, MERGED_PATH
    CONFIG_PATH = os.path.abspath(path)
    # Derive a unique cache dir from the config filename
    # e.g. config.json → cache/, config_mybunny.json → cache_mybunny/
    cfg_name = os.path.splitext(os.path.basename(path))[0]
    suffix = cfg_name.replace("config", "").strip("_- ")
    cache_name = f"cache_{suffix}" if suffix else "cache"
    CACHE_DIR = os.path.join(BASE_DIR, cache_name)
    MERGED_PATH = os.path.join(CACHE_DIR, "merged.xml")


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def fetch_xml(source: dict) -> ET.Element:
    """Download a source URL and return a parsed XML root element."""
    url = source["url"]
    logger.info(f"  Fetching: {source['name']}")
    resp = requests.get(url, timeout=60, headers={"User-Agent": "EPG-Aggregator/1.0"})
    resp.raise_for_status()

    content = resp.content
    is_gz = url.endswith(".gz")
    if is_gz:
        content = gzip.decompress(content)

    root = ET.fromstring(content)
    channels = len(root.findall("channel"))
    programmes = len(root.findall("programme"))
    logger.info(f"  ✓ {source['name']}: {channels} channels, {programmes} programmes")
    return root


def apply_channel_map(element_id: str, channel_map: dict) -> str:
    return channel_map.get(element_id, element_id)


def parse_xmltv_time(ts: str) -> datetime | None:
    """
    Parse an XMLTV timestamp into a UTC-aware datetime.
    Handles formats like:
      '20260412214500 +0000'
      '20260412214500 +0100'
      '20260412214500'
    Returns None if unparseable.
    """
    if not ts:
        return None
    ts = ts.strip()
    try:
        if len(ts) >= 19 and " " in ts:
            # e.g. '20260412214500 +0000'
            dt_part, tz_part = ts.split(" ", 1)
            dt = datetime.strptime(dt_part[:14], "%Y%m%d%H%M%S")
            # Parse timezone offset
            sign = 1 if tz_part[0] != "-" else -1
            tz_str = tz_part.lstrip("+-")
            tz_h, tz_m = int(tz_str[:2]), int(tz_str[2:4]) if len(tz_str) >= 4 else 0
            from datetime import timedelta, timezone as tz
            offset = timedelta(hours=tz_h, minutes=tz_m) * sign
            return dt.replace(tzinfo=tz(offset)).astimezone(tz.utc)
        else:
            dt = datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
            from datetime import timezone as tz
            return dt.replace(tzinfo=tz.utc)
    except Exception:
        return None


def filter_past_programmes(merged: ET.Element, keep_past_hours: int = 2) -> int:
    """
    Remove programme elements whose stop time is more than keep_past_hours ago.
    Returns the number of programmes removed.
    """
    from datetime import timezone as tz, timedelta
    cutoff = datetime.now(tz.utc) - timedelta(hours=keep_past_hours)
    to_remove = []
    for prog in merged.findall("programme"):
        stop_ts = prog.get("stop", "") or prog.get("start", "")
        stop_dt = parse_xmltv_time(stop_ts)
        if stop_dt and stop_dt < cutoff:
            to_remove.append(prog)
    for prog in to_remove:
        merged.remove(prog)
    return len(to_remove)


_COUNTRY_RE = re.compile(
    r"\b(usa|us|ca|uk|gb|au|ro|br|ph|co|rs|nz|de|fr|es|it|nl|be|mx|za|in|sg|my|th|vn|hk|tw|jp|kr)\s*$"
)
_DIRECTIONAL_RE = re.compile(
    r"\b(east|west|pacific|atlantic|north|south|central|eastern|western)\b"
)
_FILLER_RE = re.compile(
    r"\b(channel|network|television|tv|cable|hd|sd|the)\s*$"
)
_QUALITY_SUFFIXES = [" hd", " sd", " fhd", " 4k", " uhd", " +1", " plus"]


def _strip_quality(name: str) -> str:
    for token in _QUALITY_SUFFIXES:
        if name.endswith(token):
            return name[: -len(token)].strip()
    return name


def _strip_filler(name: str) -> str:
    """Strip trailing quality/filler words repeatedly until stable."""
    while True:
        new = _FILLER_RE.sub("", name).strip()
        new = _strip_quality(new)
        if new == name:
            break
        name = new
    return name


def normalize_name(name: str) -> str:
    """
    Normalize a channel display name for fuzzy matching across sources.
    e.g. "HBO (East) US"       → "hbo east"
         "Fox News Channel HD"  → "fox news"
         "A&E Network HD"       → "ae"
         "TLC HD (US)"          → "tlc"
         "FS1 Fox Sports 1 HD"  → "fox sports1"
         "HBO HD East"          → "hbo east"
    """
    name = name.lower().strip()
    # Clean special chars: A&E → ae, E! → e entertainment (handled below)
    name = name.replace("&", "").replace("!", "")
    # Strip leading "the " and known channel-code prefixes (explicit list only)
    name = re.sub(r"^the\s+", "", name)
    name = re.sub(r"^fs1\s+", "", name)
    name = re.sub(r"^fs2\s+", "", name)
    # First-pass quality strip (catches trailing HD before parens)
    name = _strip_quality(name)
    # Expand parenthetical content: "(East)" → "east", "(US)" → "us"
    name = re.sub(r"\(([^)]+)\)", r" \1 ", name)
    # Strip non-alphanumeric (keep spaces)
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    # Remove quality words anywhere in the name (not just trailing)
    # so "hbo hd east" → "hbo east", "cnn hd us" → "cnn us"
    name = re.sub(r"\b(hd|sd|fhd|4k|uhd)\b", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Compact "word digit" → "worddigit" so "HBO 2" == "HBO2"
    name = re.sub(r"([a-z]) (\d)", r"\1\2", name)
    # Strip trailing country codes: "hbo east us" → "hbo east"
    name = _COUNTRY_RE.sub("", name).strip()
    # Strip trailing quality/filler words until stable: "tlc hd" → "tlc", "fox news channel" → "fox news"
    name = _strip_filler(name)
    return name


def _normalize_nodirectional(name: str) -> str:
    """normalize_name plus directional word removal and re-applied filler strip."""
    result = re.sub(r"\s+", " ", _DIRECTIONAL_RE.sub("", normalize_name(name))).strip()
    # Re-apply filler after directional removal: "lifetime network" (after "east" removed) → "lifetime"
    result = _strip_filler(result)
    return result


# ---------------------------------------------------------------------------
# Call-sign extraction — bridges sources that use FCC call signs as IDs
# (e.g. GlobeTVApp "KDFWDT.us") with masters that embed them in display
# names like "FOX (KDFW) Dallas TX" or IDs like "fox.kdfw.dallas.tx.us".
# ---------------------------------------------------------------------------

_CALLSIGN_PAREN_RE = re.compile(r"\(([A-Z]{3,5}(?:DT\d?)?)\)")
_CALLSIGN_ID_RE = re.compile(r"^[A-Z]{1,5}(?:DT\d*)?\.us$", re.IGNORECASE)


def _extract_callsign_from_name(display_name: str) -> str | None:
    """Extract call sign from parenthetical: 'FOX (KDFW) Dallas TX' → 'kdfw'."""
    m = _CALLSIGN_PAREN_RE.search(display_name)
    if m:
        return re.sub(r"dt\d*$", "", m.group(1).lower())
    return None


def _extract_callsign_from_id(ch_id: str) -> str | None:
    """
    Extract call sign from a call-sign-style ID: 'KDFWDT.us' → 'kdfw'.
    Only matches IDs that look like FCC call signs (not 'fox.kdfw.dallas.tx.us').
    """
    if _CALLSIGN_ID_RE.match(ch_id):
        base = ch_id.lower().split(".")[0]
        return re.sub(r"dt\d*$", "", base)
    return None


def _build_name_lookup(root: ET.Element) -> dict:
    """
    Build a dict of normalized_display_name → channel_id from an XML root.
    Only the first occurrence of a normalized name is kept.
    """
    lookup = {}
    for ch in root.findall("channel"):
        ch_id = ch.get("id", "")
        for name_el in ch.findall("display-name"):
            if name_el.text:
                norm = normalize_name(name_el.text)
                if norm and norm not in lookup:
                    lookup[norm] = ch_id
    return lookup


def _normalize_start_key(start_str: str) -> str:
    """
    Normalise a programme start timestamp to a UTC string for dedup.
    This prevents duplicates when two sources report the same programme
    with different timezone offsets (e.g. '+0000' vs '-0500').
    Falls back to the raw string if parsing fails.
    """
    dt = parse_xmltv_time(start_str)
    if dt is not None:
        return dt.strftime("%Y%m%d%H%M%S")
    return start_str


def merge_standard(sources_data: list, channel_map: dict) -> ET.Element:
    """
    Standard merge: combine all sources, dedup channels and programmes by ID.
    Priority = order of sources_data (lowest priority number first).
    """
    merged = ET.Element("tv")
    merged.set("generator-info-name", "EPG-Aggregator")
    merged.set("generated-ts", datetime.utcnow().isoformat())

    seen_channels: set = set()
    seen_programmes: set = set()

    for source, root in sources_data:
        for ch in root.findall("channel"):
            raw_id = ch.get("id", "")
            mapped_id = apply_channel_map(raw_id, channel_map)
            if mapped_id in seen_channels:
                continue
            seen_channels.add(mapped_id)
            new_ch = deepcopy(ch)
            new_ch.set("id", mapped_id)
            merged.append(new_ch)

        for prog in root.findall("programme"):
            raw_ch = prog.get("channel", "")
            mapped_ch = apply_channel_map(raw_ch, channel_map)
            start = prog.get("start", "")
            key = (mapped_ch, _normalize_start_key(start))
            if key in seen_programmes:
                continue
            seen_programmes.add(key)
            new_prog = deepcopy(prog)
            new_prog.set("channel", mapped_ch)
            merged.append(new_prog)

    return merged


def merge_with_master(
    master_data: tuple,
    secondary_sources: list,
    channel_map: dict,
    skip_master_programme_patterns: list | None = None,
) -> ET.Element:
    """
    Master-source merge:
    - Master defines the channel list and IDs (the truth).
    - Secondary sources match their channels to the master by display name
      and contribute programme data under the master's channel IDs.
    - Channels in secondary sources that cannot be matched are ignored.
    - channel_map is applied ONLY at the final output stage to remap master
      IDs to M3U tvg-id values.  Internally, matching always uses the
      master's original IDs so that secondary sources can find them.
    - skip_master_programme_patterns: list of fnmatch patterns; master programme
      data is NOT added for matching channel IDs, leaving a gap for secondaries.

    Secondary-source matching rules (strictness to avoid wrong-feed data):
    - A secondary channel is matched to a master channel ONLY when the
      normalized name (including directional words like East/West) matches
      exactly.  A non-directional secondary name like "HBO2" will NOT be
      fanned out to directional master channels because we cannot know
      which feed (East vs West) the secondary data represents.
    - Non-directional secondary names may still match non-directional master
      channels (e.g. international variants without East/West).
    """
    master_source, master_root = master_data
    skip_patterns = skip_master_programme_patterns or []

    merged = ET.Element("tv")
    merged.set("generator-info-name", "EPG-Aggregator")
    merged.set("generated-ts", datetime.utcnow().isoformat())

    # Build a reverse lookup: channel_map value → channel_map key
    # so we can also match secondary source IDs that happen to equal
    # a channel_map *value* (i.e. an M3U tvg-id) back to the master ID.
    reverse_channel_map: dict = {}
    for k, v in channel_map.items():
        if v not in reverse_channel_map:
            reverse_channel_map[v] = k

    # --- Build master channel list ---
    master_ids: set = set()               # original master channel IDs
    name_to_master_id: dict = {}          # normalized name → master channel ID
    callsign_to_master_id: dict = {}      # FCC call sign → master channel ID

    # Track which master channels have directional variants so we know
    # when it's safe to use nodirectional fallback matching.
    # nd_name → set of master IDs that share that nodirectional name
    _nd_groups: dict = {}

    for ch in master_root.findall("channel"):
        raw_id = ch.get("id", "")
        output_id = apply_channel_map(raw_id, channel_map)

        new_ch = deepcopy(ch)
        new_ch.set("id", output_id)
        merged.append(new_ch)
        master_ids.add(raw_id)

        for name_el in ch.findall("display-name"):
            if name_el.text:
                norm = normalize_name(name_el.text)
                if norm:
                    existing = name_to_master_id.get(norm)
                    # Prefer .us channels over other regions on collision
                    if not existing or (raw_id.endswith(".us") and not existing.endswith(".us")):
                        name_to_master_id[norm] = raw_id
                nd = _normalize_nodirectional(name_el.text)
                if nd:
                    _nd_groups.setdefault(nd, set()).add(raw_id)
                # Extract FCC call sign from display name like "FOX (KDFW) Dallas TX"
                cs = _extract_callsign_from_name(name_el.text)
                if cs and cs not in callsign_to_master_id:
                    callsign_to_master_id[cs] = raw_id

    # Build a set of nodirectional names that have multiple master channels
    # (i.e. directional variants exist).  For these, we must NOT allow a
    # non-directional secondary name to match — it would be ambiguous.
    _ambiguous_nd: set = {nd for nd, ids in _nd_groups.items() if len(ids) > 1}

    # Nodirectional fallback: only for nd names that are NOT ambiguous
    # (i.e. only one master channel has that nd name).
    nodirectional_to_master_id: dict = {}
    for nd, ids in _nd_groups.items():
        if len(ids) == 1:
            nodirectional_to_master_id[nd] = next(iter(ids))

    logger.info(
        f"  Master '{master_source['name']}': {len(master_ids)} channels indexed, "
        f"{len(callsign_to_master_id)} call signs, "
        f"{len(_ambiguous_nd)} ambiguous nd groups blocked"
    )

    # --- Add master programmes ---
    seen_programmes: set = set()
    skipped_master_progs = 0
    master_channels_with_progs: set = set()  # track which master channels have data

    for prog in master_root.findall("programme"):
        raw_ch = prog.get("channel", "")

        # Skip master programme data for channels in the skip list so that
        # secondary sources (e.g. epg.pw) can fill them with better data.
        if skip_patterns and any(fnmatch.fnmatch(raw_ch, pat) for pat in skip_patterns):
            skipped_master_progs += 1
            continue

        start = prog.get("start", "")
        key = (raw_ch, _normalize_start_key(start))
        if key in seen_programmes:
            continue
        seen_programmes.add(key)
        new_prog = deepcopy(prog)
        new_prog.set("channel", apply_channel_map(raw_ch, channel_map))
        merged.append(new_prog)
        master_channels_with_progs.add(raw_ch)

    if skipped_master_progs:
        logger.info(
            f"  Master: skipped {skipped_master_progs} programme entries for "
            f"{len(skip_patterns)} skip-pattern(s) — secondaries will fill these channels"
        )

    # --- Collect all prefer_for patterns across sources ---
    all_prefer_patterns: list = []
    for source, root in secondary_sources:
        for pat in source.get("prefer_for", []):
            if pat not in all_prefer_patterns:
                all_prefer_patterns.append(pat)

    def _is_votable(master_id: str) -> bool:
        """True if this channel is covered by any source's prefer_for."""
        return any(fnmatch.fnmatch(master_id, p) for p in all_prefer_patterns)

    # --- Process secondary sources: collect channel mappings and votes ---
    # For votable channels we collect ALL candidates from every source,
    # then pick the winner by consensus after all sources are processed.
    # For non-votable channels we keep the fast first-in-wins dedup.

    # votes[(master_id, norm_start)] → list of (title_lower, programme_element, source_name)
    votes: dict = {}

    for source, root in secondary_sources:
        src_to_master: dict = {}  # src_id → single master_id

        for ch in root.findall("channel"):
            src_id = ch.get("id", "")

            # 1a. If the secondary ID is itself a master ID, direct match
            if src_id in master_ids:
                src_to_master[src_id] = src_id
                continue

            # 1b. Explicit channel_map
            explicit = channel_map.get(src_id, "")
            if explicit and explicit in master_ids:
                src_to_master[src_id] = explicit
                continue

            # 1c. Reverse channel_map
            reverse = reverse_channel_map.get(src_id, "")
            if reverse and reverse in master_ids:
                src_to_master[src_id] = reverse
                continue

            # 2. Name-based matching — strict directional-aware matching
            for name_el in ch.findall("display-name"):
                if name_el.text:
                    norm = normalize_name(name_el.text)

                    # Exact normalized match (includes directional words)
                    if norm in name_to_master_id:
                        src_to_master[src_id] = name_to_master_id[norm]
                        break

                    # Nodirectional fallback — ONLY when unambiguous
                    nd = _normalize_nodirectional(name_el.text)
                    if nd and nd not in _ambiguous_nd and nd in nodirectional_to_master_id:
                        src_to_master[src_id] = nodirectional_to_master_id[nd]
                        break
            else:
                # 3. Call-sign matching
                cs = _extract_callsign_from_id(src_id)
                if cs and cs in callsign_to_master_id:
                    src_to_master[src_id] = callsign_to_master_id[cs]

        matched_channels = len(src_to_master)
        added_programmes = 0

        for prog in root.findall("programme"):
            src_ch = prog.get("channel", "")
            if src_ch not in src_to_master:
                continue
            master_id = src_to_master[src_ch]
            start = prog.get("start", "")
            norm_start = _normalize_start_key(start)
            key = (master_id, norm_start)

            if _is_votable(master_id):
                # Collect vote — don't write yet
                title_el = prog.find("title")
                title = ((title_el.text or "") if title_el is not None else "").strip().lower()
                new_prog = deepcopy(prog)
                new_prog.set("channel", apply_channel_map(master_id, channel_map))
                votes.setdefault(key, []).append((title, new_prog, source["name"]))
                added_programmes += 1
            else:
                # Non-votable: first-in-wins
                if key in seen_programmes:
                    continue
                seen_programmes.add(key)
                new_prog = deepcopy(prog)
                new_prog.set("channel", apply_channel_map(master_id, channel_map))
                merged.append(new_prog)
                added_programmes += 1

        logger.info(
            f"  '{source['name']}': matched {matched_channels} channels, "
            f"contributed {added_programmes} new programmes"
        )

    # --- Resolve votes by consensus for votable channels ---
    # For each time slot, pick the title that the most sources agree on.
    # If there's a tie, the candidate from the highest-priority (earliest
    # processed) source wins.
    consensus_added = 0
    consensus_replaced = 0

    # Build index of existing programme elements for fast replacement
    _prog_index: dict = {}  # (output_channel, norm_start) → element
    if votes:
        for el in merged.findall("programme"):
            pk = (el.get("channel", ""), _normalize_start_key(el.get("start", "")))
            _prog_index[pk] = el

    for key, candidates in votes.items():
        master_id, norm_start = key

        # Count how many sources back each title
        title_counts: dict = {}   # title_lower → count
        title_first: dict = {}    # title_lower → first programme element seen
        for title, prog_el, src_name in candidates:
            title_counts[title] = title_counts.get(title, 0) + 1
            if title not in title_first:
                title_first[title] = prog_el

        # Pick winner: highest vote count, then first-seen breaks ties
        best_title = max(title_counts, key=lambda t: title_counts[t])
        winner = title_first[best_title]
        output_id = winner.get("channel", "")
        idx_key = (output_id, norm_start)

        if key in seen_programmes:
            # Replace existing programme with consensus winner
            old_el = _prog_index.get(idx_key)
            if old_el is not None:
                merged.remove(old_el)
                consensus_replaced += 1
            merged.append(winner)
            _prog_index[idx_key] = winner
            consensus_added += 1
        else:
            seen_programmes.add(key)
            merged.append(winner)
            _prog_index[idx_key] = winner
            consensus_added += 1

    if votes:
        logger.info(
            f"  Consensus voting: {len(votes)} slots evaluated, "
            f"{consensus_added} programmes written, "
            f"{consensus_replaced} replaced from earlier sources"
        )

    # --- Backfill: re-add master programmes for skipped channels that
    #     secondaries failed to fill.  This ensures directional channels
    #     (East/West) always have SOME data even if no secondary could
    #     provide a direction-specific match. ---
    if skip_patterns:
        backfilled = 0
        for prog in master_root.findall("programme"):
            raw_ch = prog.get("channel", "")
            if not any(fnmatch.fnmatch(raw_ch, pat) for pat in skip_patterns):
                continue
            # Only backfill if no secondary contributed to this channel
            start = prog.get("start", "")
            key = (raw_ch, _normalize_start_key(start))
            if key in seen_programmes:
                continue
            seen_programmes.add(key)
            new_prog = deepcopy(prog)
            new_prog.set("channel", apply_channel_map(raw_ch, channel_map))
            merged.append(new_prog)
            backfilled += 1
        if backfilled:
            logger.info(
                f"  Backfilled {backfilled} master programmes for skipped channels "
                f"that secondaries could not fill"
            )

    return merged


def merge(
    sources_data: list,
    channel_map: dict,
    skip_master_programme_patterns: list | None = None,
) -> ET.Element:
    """Route to master-source merge or standard merge depending on config."""
    master_data = None
    secondary = []

    for source, root in sources_data:
        if source.get("master", False):
            if master_data is None:
                master_data = (source, root)
            else:
                # If somehow two masters, treat extras as secondary
                secondary.append((source, root))
        else:
            secondary.append((source, root))

    if master_data:
        return merge_with_master(
            master_data, secondary, channel_map, skip_master_programme_patterns
        )
    else:
        return merge_standard(sources_data, channel_map)


def run_fetch() -> dict:
    """
    Main entry point: fetch all enabled sources, merge, write to cache.
    Returns a result dict that the server stores for the status page.
    """
    global last_result
    config = load_config()

    channel_map = {
        k: v
        for k, v in config.get("channel_map", {}).items()
        if not k.startswith("_")
    }

    sources = sorted(
        [s for s in config.get("sources", []) if s.get("enabled", True)],
        key=lambda s: s.get("priority", 99),
    )

    # Merge in auto-discovered sources
    discovered_path = os.path.join(BASE_DIR, "discovered_sources.json")
    if os.path.exists(discovered_path):
        try:
            with open(discovered_path, encoding="utf-8") as f:
                disc = json.load(f)
            disc_sources = [s for s in disc.get("sources", []) if s.get("enabled", True)]
            # Avoid duplicates by URL
            existing_urls = {s["url"] for s in sources}
            added = 0
            for ds in disc_sources:
                if ds["url"] not in existing_urls:
                    sources.append(ds)
                    existing_urls.add(ds["url"])
                    added += 1
            if added:
                sources.sort(key=lambda s: s.get("priority", 99))
                logger.info(f"  Loaded {added} auto-discovered source(s)")
        except Exception as exc:
            logger.warning(f"  Could not load discovered sources: {exc}")

    logger.info(f"Fetching {len(sources)} source(s)...")
    sources_data = []
    errors = []

    for source in sources:
        try:
            root = fetch_xml(source)
            sources_data.append((source, root))
        except Exception as exc:
            logger.error(f"  ✗ {source['name']}: {exc}")
            errors.append({"source": source["name"], "error": str(exc)})

    if not sources_data:
        msg = "All EPG sources failed to fetch."
        logger.error(msg)
        last_result = {"status": "error", "error": msg, "timestamp": datetime.utcnow().isoformat()}
        return last_result

    skip_patterns = config.get("master_skip_programme_channels", [])
    if skip_patterns:
        logger.info(f"  master_skip_programme_channels: {skip_patterns}")

    logger.info("Merging sources...")
    merged = merge(sources_data, channel_map, skip_master_programme_patterns=skip_patterns)

    # Strip old programmes
    keep_past_hours = config.get("keep_past_hours", 2)
    dropped = filter_past_programmes(merged, keep_past_hours)
    if dropped:
        logger.info(f"  Dropped {dropped} past programmes (older than {keep_past_hours}h ago)")

    os.makedirs(CACHE_DIR, exist_ok=True)
    tree = ET.ElementTree(merged)
    ET.indent(tree, space="  ")
    tree.write(MERGED_PATH, xml_declaration=True, encoding="UTF-8")

    channel_count = len(merged.findall("channel"))
    programme_count = len(merged.findall("programme"))
    logger.info(
        f"Done. Merged EPG: {channel_count} channels, {programme_count} programmes → {MERGED_PATH}"
    )

    last_result = {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "channels": channel_count,
        "programmes": programme_count,
        "sources_ok": len(sources_data),
        "sources_failed": len(errors),
        "errors": errors,
    }
    return last_result


def parse_m3u(url: str) -> list:
    """
    Fetch and parse an M3U playlist URL.
    Returns a list of dicts: {name, tvg_id, tvg_name, group}
    """
    resp = requests.get(url, timeout=30, headers={"User-Agent": "EPG-Aggregator/1.0"})
    resp.raise_for_status()
    channels = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line.startswith("#EXTINF:"):
            continue
        tvg_id_m   = re.search(r'tvg-id="([^"]*)"', line)
        tvg_name_m = re.search(r'tvg-name="([^"]*)"', line)
        group_m    = re.search(r'group-title="([^"]*)"', line)
        display    = line.rsplit(",", 1)[-1].strip() if "," in line else ""
        channels.append({
            "name":     display,
            "tvg_id":   tvg_id_m.group(1).strip()   if tvg_id_m   else "",
            "tvg_name": tvg_name_m.group(1).strip() if tvg_name_m else "",
            "group":    group_m.group(1).strip()    if group_m    else "",
        })
    return channels


def list_channels() -> list:
    """Return sorted list of {id, name} dicts from the merged cache."""
    if not os.path.exists(MERGED_PATH):
        return []
    tree = ET.parse(MERGED_PATH)
    root = tree.getroot()
    channels = []
    for ch in root.findall("channel"):
        name_el = ch.find("display-name")
        channels.append({
            "id": ch.get("id", ""),
            "name": (name_el.text or "") if name_el is not None else "",
        })
    return sorted(channels, key=lambda c: c["name"].lower())


# ---------------------------------------------------------------------------
# CLI — run the fetcher standalone (no server needed)
# Usage:
#   python fetcher.py                          # uses config.json
#   python fetcher.py --config config_mybunny.json
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Fetch and merge EPG sources")
    parser.add_argument("--config", default="config.json",
                        help="Path to config JSON file (default: config.json)")
    args = parser.parse_args()

    set_config_path(args.config)
    logger.info(f"Using config: {CONFIG_PATH}")
    logger.info(f"Cache dir   : {CACHE_DIR}")

    result = run_fetch()
    if result.get("status") == "ok":
        logger.info(f"Output: {MERGED_PATH}")
    else:
        logger.error(f"Fetch failed: {result}")
        exit(1)
