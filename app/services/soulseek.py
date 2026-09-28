"""
Soulseek (via the Nicotine+ API Nicotine Plus plugin) download service.

Drop-in alternative to services/downloader.py's yt-dlp path: given a TrackInfo,
finds and downloads a real, verified-correct FLAC (or a genuine 320kbps MP3
fallback if no FLAC exists on the network), self-healing stalled/dead sources
until every requested track is resolved.

Requires the "API Nicotine Plus" Nicotine+ plugin (local REST API) running and
enabled. See is_running() / is_api_reachable() / launch().
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from services.platform_paths import (
    DEFAULT_NICOTINE_EXE,
    NICOTINE_API_BASE_URL,
    is_nicotine_running,
)

logger = logging.getLogger(__name__)

SEARCH_WAIT_SECONDS = 22
POLL_SECONDS = 90
STALL_POLLS = 3
MAX_ATTEMPTS_PER_TRACK = 8
FLAC_ATTEMPTS_BEFORE_MP3_FALLBACK = 4

DEAD_STATUSES = {
    "File not shared.", "User logged off", "Banned (banana)",
    "Connection timeout", "Overwhelmed with requests; try again later.",
}
STOPWORDS = {
    "feat", "featuring", "remix", "mix", "edit", "version", "original", "vip",
    "the", "a", "an", "of", "to", "in", "on", "and", "&", "-", "short", "live",
}

# ─── Nicotine+ process / API lifecycle ──────────────────────────────────────


def is_running() -> bool:
    """True if a Nicotine+ process is currently running."""
    return is_nicotine_running()


def is_api_reachable(timeout: float = 3.0) -> bool:
    """True if the Nicotine+ API Nicotine Plus plugin is reachable and healthy."""
    try:
        with urllib.request.urlopen(f"{NICOTINE_API_BASE_URL}/health", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data.get("status") == "ok"
    except Exception:
        return False


def launch(wait_seconds: float = 15.0) -> dict:
    """Attempt to start Nicotine+ and wait for its API to come up.

    Returns {"launched": bool, "api_reachable": bool, "error": str | None}.
    Never raises -- the caller (sync.py) decides what an unreachable API means
    for the sync (abort with a clear message, same pattern as the Rekordbox-
    running guard).
    """
    if is_api_reachable():
        return {"launched": False, "api_reachable": True, "error": None}

    if not DEFAULT_NICOTINE_EXE or not os.path.exists(DEFAULT_NICOTINE_EXE):
        return {
            "launched": False, "api_reachable": False,
            "error": "Nicotine+ executable not found. Set NICOTINE_EXE_PATH in .env or start it manually.",
        }

    try:
        subprocess.Popen([DEFAULT_NICOTINE_EXE], close_fds=True)
    except OSError as e:
        return {"launched": False, "api_reachable": False, "error": f"Failed to start Nicotine+: {e}"}

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_api_reachable():
            return {"launched": True, "api_reachable": True, "error": None}
        time.sleep(1)

    return {
        "launched": True, "api_reachable": False,
        "error": "Nicotine+ started but its API plugin isn't responding yet. "
                 "Make sure the 'API Nicotine Plus' plugin is enabled in Preferences -> Plugins.",
    }


def get_status() -> dict:
    """Combined status for the UI: process running? API reachable?"""
    running = is_running()
    reachable = is_api_reachable() if running else False
    return {"running": running, "api_reachable": reachable}


# ─── Nicotine+ API wrapper ──────────────────────────────────────────────────


def _api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{NICOTINE_API_BASE_URL}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _api_post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{NICOTINE_API_BASE_URL}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch_all_results(token: int, max_offset: int = 4000) -> list[dict]:
    items, offset, total = [], 0, None
    while True:
        page = _api_get(f"/search/results?token={token}&limit=1000&offset={offset}")
        page_items = page.get("items", [])
        total = page.get("total", len(page_items))
        items.extend(page_items)
        offset += 1000
        if offset >= total or offset >= max_offset or not page_items:
            break
    return items


def get_downloads(active_only: bool = False) -> list[dict]:
    return _api_get(f"/downloads?active_only={'true' if active_only else 'false'}").get("items", [])


def enqueue(item: dict) -> dict:
    attrs = item.get("file_attributes") or {}
    return _api_post("/downloads/enqueue", {
        "username": item.get("username"), "virtual_path": item.get("file_path"),
        "size": item.get("size") or 0, "file_attributes": attrs,
    })


# ─── Verified matching: exact title + remix/version guard ──────────────────
# The naive approach (does the filename CONTAIN the search words?) reliably
# grabs the wrong song -- a coincidentally-matching album/folder name, a
# different remix, or just an unrelated track that happens to share a common
# word. These two checks are what actually keep matches correct.

_LEADING_NUM_RE = re.compile(r"^\s*(?:[\(\[]?[a-zA-Z]?\d{1,4}[\)\]]?[\.\-_\s]+){1,2}")
_PARENS_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_FEAT_RE = re.compile(r"\b(feat\.?|featuring|ft\.?)\b.*$", re.IGNORECASE)
_QUALIFIER_WORDS = {
    "remix", "rmx", "rework", "bootleg", "mashup", "flip", "vip",
    "edit", "extended", "radio", "original", "version", "mix",
    "live", "acoustic", "instrumental", "acapella",
}
_CONNECTOR_WORDS = {"to", "a", "an", "of", "the", "and", "&", "in", "on"}
_PUNCT_RE = re.compile(r"[^a-z0-9]+")

_ALT_VERSION_MARKERS = [
    "remix", "rmx", "rework", "bootleg", "mashup", "flip", "vip",
    "acoustic", "live", "acapella", "instrumental",
]
# Deliberately NOT included: "extended", "radio edit" -- length variants of
# the SAME artist's mix, not a different artist's derivative version.
_SAFE_PHRASES = ["original mix", "original version", "album version"]


def _clean_tokens(text: str, extra_strip_words=()) -> set[str]:
    text = _PARENS_RE.sub(" ", text)
    text = _FEAT_RE.sub("", text)
    text = _PUNCT_RE.sub(" ", text.lower())
    words = text.split()
    strip_set = _QUALIFIER_WORDS | _CONNECTOR_WORDS | {w.lower() for w in extra_strip_words}
    return {w for w in words if w not in strip_set and len(w) > 1 and not w.isdigit()}


def _artist_words(artist_full: str) -> list[str]:
    words = []
    for a in re.split(r"[,&/]", artist_full):
        words.extend(re.findall(r"[a-zA-Z0-9]+", a))
    return words


def is_exact_title_match(file_path: str, artist_full: str, title_main: str) -> bool:
    """True only if the candidate's actual track-title portion (after stripping
    track numbers, artist name, and any parenthetical version tag) is EXACTLY
    the target title -- not merely containing its words."""
    filename = re.split(r"[\\/]", file_path)[-1]
    stem = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", filename)
    a_words = _artist_words(artist_full)

    target_tokens = _clean_tokens(title_main)
    if not target_tokens:
        return False

    segments = re.split(r"\s+-\s+", stem)
    if len(segments) >= 2:
        artist_token_set = {w.lower() for w in a_words if len(w) > 1}

        def is_track_number(seg):
            return bool(re.fullmatch(r"[\(\[]?[a-zA-Z]?\d{1,4}[\)\]]?\.?", seg.strip()))

        def is_artist_segment(seg):
            seg_tokens = _clean_tokens(seg)
            return bool(seg_tokens) and seg_tokens.issubset(artist_token_set)

        remaining = [seg for seg in segments if not is_track_number(seg) and not is_artist_segment(seg)]
        if not remaining:
            remaining = list(segments)
        candidates_to_check = remaining if len(remaining) == 1 else [remaining[-1]]
    else:
        candidates_to_check = [_LEADING_NUM_RE.sub("", stem)]

    for cand in candidates_to_check:
        cand_no_num = _LEADING_NUM_RE.sub("", cand)
        cand_tokens = _clean_tokens(cand_no_num, extra_strip_words=a_words)
        if cand_tokens == target_tokens:
            return True
    return False


def _extract_qualifier(title_full: str) -> str | None:
    parts = re.split(r"\s+-\s+", title_full, maxsplit=1)
    if len(parts) == 2:
        return parts[1].strip()
    m = re.search(r"\(([^)]*(?:remix|mix|edit|rework|vip|flip|mashup)[^)]*)\)", title_full, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _qualifier_tokens(qualifier: str | None) -> list[str]:
    if not qualifier:
        return []
    words = re.findall(r"[a-z0-9]+", qualifier.lower())
    return [w for w in words if w not in {"remix", "mix", "the", "a", "of", "edit", "version"} and len(w) > 1]


def passes_version_guard(file_path_lower: str, title_full: str) -> bool:
    """True if this candidate is an acceptable version match for the title:
    a plain title rejects an unrequested remix/rework/etc; a title that names
    a specific remix requires that remixer's name to actually appear."""
    qualifier = _extract_qualifier(title_full)
    if any(safe in file_path_lower for safe in _SAFE_PHRASES):
        return True
    has_alt_marker = any(marker in file_path_lower for marker in _ALT_VERSION_MARKERS)
    if not qualifier:
        return not has_alt_marker
    q_tokens = _qualifier_tokens(qualifier)
    if not q_tokens:
        return True
    return any(tok in file_path_lower for tok in q_tokens)


def _build_query(artist_full: str, title_full: str) -> str:
    primary_artist = artist_full.split(",")[0].strip()
    title_main = re.split(r"\s+-\s+", title_full, maxsplit=1)[0]
    return f"{primary_artist} {title_main}".strip()


def find_candidate(artist_full: str, title_full: str, mode: str, exclude_users: set[str]) -> tuple[dict | None, int]:
    """One search attempt. mode: 'flac' or 'mp3'. Returns (best candidate or
    None, number of raw results seen) -- the count is surfaced to callers so
    "zero results" (live-network luck) can be told apart from "results came
    back but none verified.\""""
    query = _build_query(artist_full, title_full)
    title_main = re.split(r"\s+-\s+", title_full, maxsplit=1)[0]

    resp = _api_post("/search", {"query": query, "mode": "global"})
    time.sleep(SEARCH_WAIT_SECONDS)
    items = _fetch_all_results(resp["token"])

    cands = []
    for it in items:
        fp = it.get("file_path") or ""
        if it.get("username") in exclude_users:
            continue
        if not is_exact_title_match(fp, artist_full, title_main):
            continue
        if not passes_version_guard(fp.lower(), title_full):
            continue
        attrs = it.get("file_attributes") or {}
        if mode == "flac":
            if not fp.lower().endswith(".flac") or attrs.get("5") is None:
                continue
        else:
            if not fp.lower().endswith(".mp3") or attrs.get("0") != 320:
                continue
        cands.append(it)

    cands.sort(key=lambda i: (bool(i.get("free_upload_slots")), i.get("upload_speed") or 0, i.get("size") or 0), reverse=True)
    return (cands[0] if cands else None), len(items)


# ─── 16-bit FLAC conversion (archives originals, never deletes) ────────────


def _probe(path: Path) -> tuple[str | None, int | None]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,bits_per_raw_sample",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    codec, bits = None, None
    for line in out.stdout.splitlines():
        if line.startswith("codec_name="):
            codec = line.split("=", 1)[1]
        elif line.startswith("bits_per_raw_sample="):
            v = line.split("=", 1)[1]
            bits = int(v) if v.isdigit() else None
    return codec, bits


def ensure_16bit_flac(path: Path, originals_dir: Path) -> Path:
    """Return a path guaranteed to be real 16-bit/44.1kHz FLAC, converting
    (and archiving the original -- never deleting it) if it isn't already.
    A 320kbps MP3 converted this way sounds identical to the MP3 (no quality
    is gained by wrapping it in FLAC) -- this is purely for format uniformity."""
    codec, bits = _probe(path)
    if codec == "flac" and bits == 16:
        return path

    originals_dir.mkdir(exist_ok=True)
    new_path = path.with_suffix(".flac") if path.suffix.lower() != ".flac" else path
    tmp_path = new_path.parent / (new_path.name + ".converting.flac")

    cmd = ["ffmpeg", "-y", "-i", str(path), "-map", "0:a:0",
           "-sample_fmt", "s16", "-ar", "44100", "-c:a", "flac", "-f", "flac", str(tmp_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        logger.warning("16-bit conversion failed for %s, keeping as-is: %s", path.name, result.stderr[-300:])
        return path

    archived = originals_dir / path.name
    shutil.move(str(path), str(archived))
    shutil.move(str(tmp_path), str(new_path))
    logger.info("Converted %s -> 16-bit FLAC (original archived to _originals/%s)", new_path.name, path.name)
    return new_path


# ─── Batch search-then-watch pipeline ───────────────────────────────────────
# Per-track state used while resolving a whole playlist's worth of new tracks.


class _TrackState:
    __slots__ = ("track", "key", "mode", "history", "tried_users", "attempts", "resolved", "downloaded")

    def __init__(self, track):
        self.track = track
        self.key = None          # (username, file_path) currently queued
        self.mode = "flac"
        self.history = []        # recent progress_pct readings, for stall detection
        self.tried_users = set()
        self.attempts = 0
        self.resolved = False
        self.downloaded = False


def search_and_queue_all(tracks: list, on_progress=None) -> dict[str, _TrackState]:
    """First pass: fire one search per track and queue whatever verified match
    (if any) turns up. Returns the per-track state the watchdog then polls."""
    states = {}
    for i, track in enumerate(tracks):
        if on_progress:
            on_progress(f"Searching Soulseek ({i + 1}/{len(tracks)}): {track.artist} - {track.title}")
        st = _TrackState(track)
        best, _ = find_candidate(track.artist, track.title, "flac", set())
        if best:
            enqueue(best)
            st.key = (best["username"], best["file_path"])
        states[track.spotify_id] = st
    return states


def resolve_all(states: dict[str, _TrackState], on_progress=None, max_wall_seconds: float = 6 * 3600) -> None:
    """Poll until every track is downloaded or given up: replaces stalled or
    dead sources with a freshly verified alternative (excluding every
    uploader already tried), falling back to a 320kbps MP3 only after several
    failed FLAC attempts. Mutates `states` in place."""
    start = time.time()

    while True:
        elapsed = time.time() - start
        by_key = {}
        for d in get_downloads(active_only=False):
            key = (d.get("username"), d.get("virtual_path") or d.get("file_path"))
            by_key[key] = d

        for st in states.values():
            if st.resolved:
                continue

            status, pct = None, 0
            if st.key:
                d = by_key.get(st.key)
                status = d.get("status") if d else "NOT FOUND"
                pct = d.get("progress_pct", 0) if d else 0

            if status == "Finished":
                st.downloaded = True
                st.resolved = True
                if on_progress:
                    on_progress(f"Downloaded: {st.track.artist} - {st.track.title}")
                continue

            needs_replacement = st.key is None
            if not needs_replacement:
                if status in DEAD_STATUSES or status == "NOT FOUND":
                    needs_replacement = True
                else:
                    st.history.append(pct)
                    st.history = st.history[-STALL_POLLS:]
                    if len(st.history) == STALL_POLLS and len(set(st.history)) == 1:
                        needs_replacement = True

            if not needs_replacement:
                continue

            if st.key:
                st.tried_users.add(st.key[0])
            st.attempts += 1
            if st.attempts > MAX_ATTEMPTS_PER_TRACK:
                st.resolved = True
                if on_progress:
                    on_progress(f"Gave up (no source found): {st.track.artist} - {st.track.title}")
                continue

            mode = st.mode
            if mode == "flac" and st.attempts > FLAC_ATTEMPTS_BEFORE_MP3_FALLBACK:
                mode = "mp3"

            best, _ = find_candidate(st.track.artist, st.track.title, mode, st.tried_users)
            if not best:
                st.mode = mode
                continue

            enqueue(best)
            st.key = (best["username"], best["file_path"])
            st.mode = mode
            st.history = []

        resolved_count = sum(1 for s in states.values() if s.resolved)
        if on_progress:
            downloaded_count = sum(1 for s in states.values() if s.downloaded)
            on_progress(f"Soulseek: {downloaded_count}/{len(states)} downloaded, {resolved_count}/{len(states)} resolved")

        if resolved_count == len(states):
            return
        if elapsed >= max_wall_seconds:
            logger.warning("Soulseek resolve_all hit the %ds wall-clock limit with tracks still pending", max_wall_seconds)
            return

        time.sleep(POLL_SECONDS)


def download_path_for(state: _TrackState, nicotine_download_dir: str) -> Path | None:
    """The local file Nicotine+ actually saved a resolved track's download to."""
    if not state.downloaded or not state.key:
        return None
    _, file_path = state.key
    local = Path(nicotine_download_dir) / Path(file_path).name
    if local.exists():
        return local
    # Nicotine+ silently strips leading whitespace from saved filenames
    stripped = Path(nicotine_download_dir) / Path(file_path).name.strip()
    if stripped.exists():
        return stripped
    return None
