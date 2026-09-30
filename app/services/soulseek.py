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
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

from services import audio_formats
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
LOSSLESS_ATTEMPTS_BEFORE_MP3_FALLBACK = 4

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


def _clean_words(text: str, extra_strip_words=()) -> list[str]:
    text = _PARENS_RE.sub(" ", text)
    text = _FEAT_RE.sub("", text)
    text = re.sub(r"(?<=\w)['’](?=\w)", "", text)     # what's / whats / what’s are one word
    text = _PUNCT_RE.sub(" ", text.lower())
    strip_set = _QUALIFIER_WORDS | _CONNECTOR_WORDS | {w.lower() for w in extra_strip_words}
    # Digits are part of a title ("Liquid Interlude 2" is not "Liquid Interlude 4"); track
    # numbers in filenames are removed separately, before this runs.
    return [w for w in text.split() if w not in strip_set and (len(w) > 1 or w.isdigit())]


def _clean_tokens(text: str, extra_strip_words=()) -> set[str]:
    return set(_clean_words(text, extra_strip_words))


def _artist_words(artist_full: str) -> list[str]:
    """Every word of the artist name, plus apostrophe-less spellings: filenames
    written as "barry_cant_swim" have no apostrophe to split "Can't" into "can"+"t"."""
    words = []
    for a in re.split(r"[,&/]", artist_full):
        words.extend(re.findall(r"[a-zA-Z0-9]+", a))
        words.extend(re.findall(r"[a-zA-Z0-9]+", re.sub(r"['’]", "", a)))
    return words


def is_exact_title_match(file_path: str, artist_full: str, title_main: str) -> bool:
    """True only if the candidate's actual track-title portion (after stripping
    track numbers, artist name, and any parenthetical version tag) is EXACTLY
    the target title -- not merely containing its words."""
    filename = re.split(r"[\\/]", file_path)[-1]
    stem = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", filename)
    a_words = _artist_words(artist_full)

    # Artist words are stripped from BOTH sides. Stripping only the filename side broke
    # any title sharing a word with the artist's name ("Can We Still Be Friends" by
    # "Barry Can't Swim": the filename lost "can", the title kept it, no match ever).
    strip_words = a_words
    target_tokens = _clean_tokens(title_main, extra_strip_words=strip_words)
    if not target_tokens:                           # title made only of artist words
        strip_words = []
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

    target_joined = "".join(_clean_words(title_main, strip_words))
    for cand in candidates_to_check:
        # the second form keeps a leading number, for titles that start with one ("22")
        for text in (_LEADING_NUM_RE.sub("", cand), cand):
            if _clean_tokens(text, extra_strip_words=strip_words) == target_tokens:
                return True
            # "Rollercoaster" vs "Roller Coaster": same word, spaced differently
            if "".join(_clean_words(text, strip_words)) == target_joined:
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


_NUMBERED_TAG_RE = re.compile(r"\b(mix|part|pt|version|vol|volume|take)\.?\s*0*(\d+)\b")


def _numbered_tags(text: str) -> set[tuple[str, str]]:
    return {("part" if w == "pt" else w, n) for w, n in _NUMBERED_TAG_RE.findall(text.lower())}


def passes_version_guard(file_path_lower: str, title_full: str) -> bool:
    """True if this candidate is an acceptable version match for the title:
    a plain title rejects an unrequested remix/rework/etc; a title that names
    a specific remix requires that remixer's name to actually appear; and a numbered
    version ("Mix 1", "Part 2") must not be a different number."""
    filename = re.split(r"[\\/]", file_path_lower)[-1]      # not the folder: "Vol. 1" compilations
    cand_tags = _numbered_tags(filename)
    if cand_tags and cand_tags != _numbered_tags(title_full):
        return False
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


def _ext(path: str) -> str:
    m = re.search(r"\.([a-z0-9]{2,5})$", path.lower())
    return "." + m.group(1) if m else ""


# Which format we'd rather have, best first: FLAC, then WAV and AIFF as equals, then a
# 320kbps MP3 only as the last resort. This outranks everything else in choosing a
# source (a free upload slot only breaks ties WITHIN a tier).
FORMAT_TIER = {".flac": 2, ".wav": 1, ".aif": 1, ".aiff": 1, ".mp3": 0}


def _lossless_candidate(item: dict) -> tuple[int, int] | None:
    """(format tier, evidence) for a lossless candidate, or None if it isn't one.
    FLAC peers report a bit depth (attr 5), which we require. WAV/AIFF results are
    rare and usually carry no attributes at all, so they're accepted on weaker
    evidence (1 = the peer reports a bit depth or a PCM-sized bitrate, 0 = nothing)
    and PROVEN after download instead -- see resolve_all's `validate`."""
    ext = _ext(item.get("file_path") or "")
    attrs = item.get("file_attributes") or {}
    if ext == ".flac":
        return (FORMAT_TIER[".flac"], 1) if attrs.get("5") is not None else None
    if ext in (".wav", ".aif", ".aiff"):
        evidence = 1 if (attrs.get("5") is not None or (attrs.get("0") or 0) >= 700) else 0
        return FORMAT_TIER[ext], evidence
    return None


def _search(query: str) -> list[dict]:
    resp = _api_post("/search", {"query": query, "mode": "global"})
    time.sleep(SEARCH_WAIT_SECONDS)
    return _fetch_all_results(resp["token"])


def _fallback_query(artist_full: str, title_main: str) -> str | None:
    """A prefix search for a single long word ("Rollercoaster" -> "Roller"), which
    also finds files spelled "Roller Coaster". Only worth trying for those titles."""
    words = re.findall(r"[A-Za-z0-9]+", title_main)
    if len(words) != 1 or len(words[0]) < 8:
        return None
    primary_artist = artist_full.split(",")[0].strip()
    return f"{primary_artist} {words[0][:max(5, round(len(words[0]) * 0.45))]}".strip()


def _has_title_match(items: list[dict], artist_full: str, title_main: str, title_full: str) -> bool:
    return any(is_exact_title_match(i.get("file_path") or "", artist_full, title_main)
               and passes_version_guard((i.get("file_path") or "").lower(), title_full) for i in items)


class SearchStats(NamedTuple):
    raw: int        # results the network returned
    matched: int    # ...of which are this exact title (right song, right version)


def find_candidate(artist_full: str, title_full: str, mode: str, exclude_users: set[str]) -> tuple[dict | None, SearchStats]:
    """One search attempt. mode: 'lossless' (FLAC, WAV or AIFF) or 'mp3' (a genuine
    320kbps fallback). Returns (best candidate or None, SearchStats). The stats tell
    apart the three ways a search comes up empty: the network returned nothing (bad
    luck), it returned results but none was this song (usually our matching is too
    strict), or right-song files exist but none is usable (wrong format, dead peers)."""
    query = _build_query(artist_full, title_full)
    title_main = re.split(r"\s+-\s+", title_full, maxsplit=1)[0]

    items = _search(query)
    fallback = _fallback_query(artist_full, title_main)
    if fallback and not _has_title_match(items, artist_full, title_main, title_full):
        # a title spelled as one word here may be two words in people's filenames
        items = items + _search(fallback)

    cands = []
    matched = 0
    for it in items:
        fp = it.get("file_path") or ""
        if not is_exact_title_match(fp, artist_full, title_main):
            continue
        if not passes_version_guard(fp.lower(), title_full):
            continue
        matched += 1
        if it.get("username") in exclude_users:
            continue
        attrs = it.get("file_attributes") or {}
        if mode == "lossless":
            ranked = _lossless_candidate(it)
            if ranked is None:
                continue
        else:
            if _ext(fp) != ".mp3" or attrs.get("0") != 320:
                continue
            ranked = (FORMAT_TIER[".mp3"], 0)
        cands.append((it, ranked))

    # Format tier first (FLAC > WAV = AIFF > MP3), then within a tier: a free upload
    # slot (no slot = it may never start), how much evidence the peer gives, speed, size.
    cands.sort(key=lambda c: (c[1][0], bool(c[0].get("free_upload_slots")), c[1][1],
                              c[0].get("upload_speed") or 0, c[0].get("size") or 0), reverse=True)
    return (cands[0][0] if cands else None), SearchStats(len(items), matched)


# ─── Authenticity check: catch lossy-source transcodes in lossless files ────
# A file that's really an MP3 decoded and re-wrapped as FLAC/WAV/AIFF keeps the lossy
# encoder's low-pass: the spectrum falls off a CLIFF at the encoder's cutoff and sits
# flat at the noise floor above it (about 16 kHz for ~128 kbps, ~19 kHz for ~192 kbps).
# A genuine recording rolls off gradually.
#
# Calibrated on real music: 34 genuine FLACs, plus 12 of them round-tripped through MP3
# at 128/192/256/320 kbps. The drop between adjacent 1 kHz bands (16-20 kHz) never
# exceeded 12.4 dB on a genuine file, while every 128, 192 and 256 kbps transcode
# dropped 16.5 dB or more (usually 25-50). The 16 dB threshold sits in that gap: 0 of 34
# genuine files flagged, 36 of 36 transcodes at <= 256 kbps caught.
#
# What it can NOT do: catch a 320 kbps transcode (0 of 12 caught). Those keep content to
# ~20 kHz, indistinguishable from a genuine file's anti-alias filter. It's also a
# heuristic -- a genuinely lossless file with an unusually hard low-pass would be
# flagged -- so a flagged file is kept and reported, never rejected.
#
# (An earlier version measured energy above 20 kHz with ffmpeg's highpass filter. That
# filter is far too gentle: loud content just below 20 kHz leaks straight through, so a
# file deliberately low-passed at 16 kHz still measured -43 dB and passed.)

SPECTRAL_CLIFF_DB = 16.0
CLIFF_BOUNDARIES_KHZ = (16, 17, 18, 19)
_FFT_SIZE = 8192
_MIN_RATE_TO_CHECK = 40000


def _band_levels(path: Path, rate: int) -> dict[int, float] | None:
    """Average level (dB) of each 1 kHz band from 15 to 20 kHz, over the loud half
    of the track. None if the file is too short to measure."""
    import numpy as np

    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-t", "900", "-f", "f32le", "-"],
        capture_output=True, timeout=180)
    x = np.frombuffer(r.stdout, dtype=np.float32)
    n = _FFT_SIZE
    if len(x) < n * 4:
        return None

    starts = np.arange(0, len(x) - n, n)
    if len(starts) > 400:
        starts = starts[np.linspace(0, len(starts) - 1, 400).astype(int)]
    win = np.hanning(n)
    spectra = np.abs(np.fft.rfft(np.stack([x[s:s + n] * win for s in starts]), axis=1)) ** 2
    freqs = np.fft.rfftfreq(n, 1 / rate)

    # Skip silence and quiet intros: judge the spectrum on the loud half of the windows.
    mid_energy = spectra[:, (freqs >= 1000) & (freqs <= 5000)].sum(axis=1)
    mean_db = 10 * np.log10(spectra[mid_energy >= np.median(mid_energy)].mean(axis=0) + 1e-20)
    return {k: float(mean_db[(freqs >= k * 1000) & (freqs < (k + 1) * 1000)].mean()) for k in range(15, 20)}


def check_authenticity(path: Path) -> dict:
    """Returns {"suspect": bool, "cliff_khz": int | None, "drop_db": float | None, "reason": str}."""
    def result(suspect, reason, cliff=None, drop=None):
        return {"suspect": suspect, "cliff_khz": cliff, "drop_db": drop, "reason": reason}

    _, _, rate = audio_formats.probe(path)
    if not rate or rate < _MIN_RATE_TO_CHECK:
        return result(False, "sample rate too low to check")
    try:
        levels = _band_levels(path, rate)
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        return result(False, f"authenticity check failed to run: {e}")
    if levels is None:
        return result(False, "too short to measure")

    # drop across each boundary: the band just below it minus the band just above
    drops = {b: levels[b - 1] - levels[b] for b in CLIFF_BOUNDARIES_KHZ}
    cliff, drop = max(drops.items(), key=lambda kv: kv[1])
    if drop < SPECTRAL_CLIFF_DB:
        return result(False, "spectrum rolls off gradually, no lossy cutoff found", cliff, round(drop, 1))

    rough = "128 kbps or lower" if cliff <= 17 else "192-256 kbps"
    return result(
        True,
        f"hard cutoff near {cliff} kHz (a {drop:.0f} dB cliff) -- the signature of an MP3 at roughly {rough} "
        f"re-encoded as lossless, not a genuinely lossless source",
        cliff, round(drop, 1))


# ─── Batch search-then-watch pipeline ───────────────────────────────────────
# Per-track state used while resolving a whole playlist's worth of new tracks.


class _TrackState:
    __slots__ = ("track", "key", "mode", "history", "tried_users", "attempts", "resolved", "downloaded", "local_path",
                 "max_raw", "max_matched")

    def __init__(self, track):
        self.track = track
        self.key = None          # (username, file_path) currently queued
        self.mode = "lossless"   # falls back to "mp3" after repeated failures
        self.history = []        # recent progress_pct readings, for stall detection
        self.tried_users = set()
        self.attempts = 0
        self.resolved = False
        self.downloaded = False
        self.local_path = None   # set when the file was already on disk
        self.max_raw = 0         # best search so far: results returned / results that were this song
        self.max_matched = 0

    def note_search(self, stats: SearchStats) -> None:
        self.max_raw = max(self.max_raw, stats.raw)
        self.max_matched = max(self.max_matched, stats.matched)

    def why_no_source(self) -> str:
        if self.max_raw == 0:
            return "the network returned no results for it"
        if self.max_matched == 0:
            return f"{self.max_raw} results came back but none matched this title"
        return f"{self.max_matched} matching files were found but none was usable"


def _scan_local_lossless(download_dir: str | None) -> list[Path]:
    """Lossless files already sitting (top level only) in Nicotine+'s download
    folder -- e.g. from an earlier sync that was interrupted before importing them."""
    if not download_dir:
        return []
    try:
        return [p for p in Path(download_dir).iterdir()
                if p.is_file() and p.suffix.lower() in audio_formats.LOSSLESS_EXTS]
    except OSError:
        return []


def _live_lossless_downloads() -> list[dict]:
    """Lossless downloads Nicotine+ has already queued or is mid-transfer on."""
    try:
        items = get_downloads(active_only=False)
    except Exception:
        return []
    live = {"Queued", "Transferring", "Getting status", "Paused"}
    out = []
    for d in items:
        path = d.get("virtual_path") or d.get("file_path") or ""
        if d.get("status") in live and _ext(path) in audio_formats.LOSSLESS_EXTS:
            out.append(d)
    return out


def _title_matches(path: str, track) -> bool:
    title_main = re.split(r"\s+-\s+", track.title, maxsplit=1)[0]
    return is_exact_title_match(path, track.artist, title_main) and passes_version_guard(path.lower(), track.title)


def _match_local(track, local_files: list[Path], claimed: set, prefer_exts: set) -> Path | None:
    hits = [p for p in local_files
            if p not in claimed and _title_matches(p.name, track) and audio_formats.is_valid_lossless(p)]
    if not hits:
        return None
    # Same hierarchy as a fresh search: FLAC, then WAV/AIFF. Within a tier, prefer a
    # file already in a wanted container (needs no conversion), then the larger one
    # (size only means something between two files of the SAME format).
    best = max(hits, key=lambda p: (FORMAT_TIER.get(p.suffix.lower(), 0), p.suffix.lower() in prefer_exts,
                                    p.stat().st_size))
    claimed.add(best)
    return best


def _match_live(track, live: list[dict], claimed: set) -> dict | None:
    for d in live:
        path = d.get("virtual_path") or d.get("file_path") or ""
        key = (d.get("username"), path)
        if key not in claimed and _title_matches(path, track):
            claimed.add(key)
            return d
    return None


def search_and_queue_all(tracks: list, on_progress=None, local_dir: str | None = None,
                         prefer_exts: set | None = None) -> dict[str, _TrackState]:
    """First pass. For each track, reuse what Soulseek/Nicotine+ already gave us
    before spending a search on it:
      1. a matching lossless file already in the download folder -> done, no search
      2. a matching download Nicotine+ already has queued/in flight -> adopt it
         (the watchdog monitors it and replaces it if it stalls)
      3. otherwise search and queue a verified match.
    `prefer_exts` (e.g. {".flac", ".aiff"}) breaks ties toward a wanted container.
    Returns the per-track state the watchdog then polls."""
    prefer_exts = prefer_exts or set()
    local_files = _scan_local_lossless(local_dir)
    live = _live_lossless_downloads()
    claimed_local: set = set()
    claimed_live: set = set()

    states = {}
    for i, track in enumerate(tracks):
        st = _TrackState(track)
        states[track.spotify_id] = st
        label = f"{track.artist} - {track.title}"

        local = _match_local(track, local_files, claimed_local, prefer_exts)
        if local:
            st.local_path = local
            st.downloaded = True
            st.resolved = True
            if on_progress:
                on_progress(f"Already downloaded ({i + 1}/{len(tracks)}): {label}")
            continue

        adopted = _match_live(track, live, claimed_live)
        if adopted:
            st.key = (adopted["username"], adopted.get("virtual_path") or adopted.get("file_path"))
            if on_progress:
                on_progress(f"Already queued in Nicotine+ ({i + 1}/{len(tracks)}): {label}")
            continue

        if on_progress:
            on_progress(f"Searching Soulseek ({i + 1}/{len(tracks)}): {label}")
        best, stats = find_candidate(track.artist, track.title, "lossless", set())
        st.note_search(stats)
        if best:
            enqueue(best)
            st.key = (best["username"], best["file_path"])
    return states


def _saved_file(file_path: str, download_dir: str) -> Path | None:
    """Where Nicotine+ actually saved a download (it keeps only the filename, and
    silently strips leading whitespace from it)."""
    name = re.split(r"[\\/]", file_path)[-1]
    for candidate in (name, name.strip()):
        p = Path(download_dir) / candidate
        if p.exists():
            return p
    return None


def resolve_all(states: dict[str, _TrackState], on_progress=None, max_wall_seconds: float = 6 * 3600,
                download_dir: str | None = None, validate=None) -> None:
    """Poll until every track is downloaded or given up: replaces stalled, dead or
    unusable sources with a freshly verified alternative (excluding every uploader
    already tried), falling back to a 320kbps MP3 only after several failed
    lossless attempts. Mutates `states` in place.

    `validate(path) -> bool` (with `download_dir`) is the "is this finished file
    actually usable" gate: a WAV that turns out to be compressed, or not audio at
    all, is treated like a dead source instead of being handed on."""
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

            needs_replacement = False
            if status == "Finished":
                usable = True
                if validate and download_dir:
                    f = _saved_file(st.key[1], download_dir)
                    usable = bool(f) and validate(f)
                if usable:
                    st.downloaded = True
                    st.resolved = True
                    if on_progress:
                        on_progress(f"Downloaded: {st.track.artist} - {st.track.title}")
                    continue
                if on_progress:
                    on_progress(f"Unusable download, trying another source: {st.track.artist} - {st.track.title}")
                needs_replacement = True
            elif st.key is None:
                needs_replacement = True
            elif status in DEAD_STATUSES or status == "NOT FOUND":
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
                    on_progress(f"Gave up: {st.track.artist} - {st.track.title} ({st.why_no_source()})")
                continue

            mode = st.mode
            if mode == "lossless" and st.attempts > LOSSLESS_ATTEMPTS_BEFORE_MP3_FALLBACK:
                mode = "mp3"

            best, stats = find_candidate(st.track.artist, st.track.title, mode, st.tried_users)
            st.note_search(stats)
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
    """The local file for a resolved track: one that was already on disk, or the
    file Nicotine+ saved for its download."""
    if state.local_path and Path(state.local_path).exists():
        return Path(state.local_path)
    if not state.downloaded or not state.key:
        return None
    return _saved_file(state.key[1], nicotine_download_dir)
