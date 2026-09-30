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
MAX_ATTEMPTS_PER_TRACK = 6
# After this many lossless-only searches the search also accepts a genuine 320 kbps MP3.
# The fallback still ranks lossless first, so it costs nothing to start it early: it only
# ever adds MP3 as a last choice. (Re-running the same lossless-only search finds the same
# things; results do vary between searches, which the later attempts still benefit from.)
LOSSLESS_ATTEMPTS_BEFORE_MP3_FALLBACK = 2
# ...but a song of which not a single file has ever shown up is dropped sooner-than-never,
# after this many searches, since more of the same 90 seconds apart only wastes time.
GIVE_UP_IF_NOTHING_AFTER = 4

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


# Nicotine+ is a busy desktop app (its memory grew past 4 GB over a long session) and its
# API now and then stalls for a moment: one "504 Gateway Timeout" on a search request used
# to kill a sync that had been running for hours. Temporary failures are retried, with
# growing pauses; anything else (a bad request, a 404) fails immediately as before.
API_RETRY_DELAYS = (3, 10, 30, 60)
API_TIMEOUT_SECONDS = 30
_TRANSIENT_HTTP_CODES = {500, 502, 503, 504}


def _with_retries(call):
    for attempt in range(len(API_RETRY_DELAYS) + 1):
        try:
            return call()
        except urllib.error.HTTPError as e:
            if e.code not in _TRANSIENT_HTTP_CODES or attempt == len(API_RETRY_DELAYS):
                raise
            problem = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == len(API_RETRY_DELAYS):
                raise
            problem = str(e)
        logger.warning("Nicotine+ API hiccup (%s); retrying in %ds", problem, API_RETRY_DELAYS[attempt])
        time.sleep(API_RETRY_DELAYS[attempt])


def _api_get(path: str) -> dict:
    def call():
        with urllib.request.urlopen(f"{NICOTINE_API_BASE_URL}{path}", timeout=API_TIMEOUT_SECONDS) as r:
            return json.loads(r.read().decode("utf-8"))
    return _with_retries(call)


def _api_post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")

    def call():
        req = urllib.request.Request(
            f"{NICOTINE_API_BASE_URL}{path}", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as r:
            return json.loads(r.read().decode("utf-8"))
    return _with_retries(call)


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


# Words that describe a KIND of version rather than whose version it is.
_GENERIC_QUALIFIER_WORDS = {"dub", "instrumental", "extended", "radio", "original", "club", "vip", "version",
                            "remix", "mix", "edit", "dubstrumental", "and", "with", "feat", "featuring"}


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
    if not qualifier:
        # a plain title: "(Original Mix)" is fine, an unrequested remix/live/etc. is not
        if any(safe in file_path_lower for safe in _SAFE_PHRASES):
            return True
        return not any(marker in file_path_lower for marker in _ALT_VERSION_MARKERS)

    # A title that names a version needs THAT version. "(Original Mix)" is no longer an
    # escape hatch here: it is the one version this title is not.
    q_tokens = _qualifier_tokens(qualifier)
    if not q_tokens:
        return True
    # Every distinctive word of the tag has to be there ("ickle", "prince fatty",
    # "subatomic sound system"); the old "any one word will do" accepted a plain
    # "Standing Firm" for "Standing Firm (ickle's Dub Mix)" because the path said "dub".
    distinctive = [t for t in q_tokens if t not in _GENERIC_QUALIFIER_WORDS]
    if distinctive:
        return all(tok in file_path_lower for tok in distinctive)
    return any(tok in file_path_lower for tok in q_tokens)


def _query_title(title_main: str) -> str:
    """The title as it should appear in a search. Soulseek returns only files matching
    EVERY term, so anything people don't put in filenames must go: a "(feat. X)" or
    "(with X & Y)" credit, a "(... Dub Mix)" tag (the version is checked on the results
    instead), quote marks and ampersands. Left in, "Green Brain (with Lee "Scratch"
    Perry & Yaadcore)" matches nothing at all."""
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title_main)
    text = re.sub(r'["\u201c\u201d&]', " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or title_main.strip()


def _build_query(artist_full: str, title_full: str) -> str:
    primary_artist = artist_full.split(",")[0].strip()
    title_main = re.split(r"\s+-\s+", title_full, maxsplit=1)[0]
    return f"{primary_artist} {_query_title(title_main)}".strip()


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


# Searches overlap: post a batch, wait ONCE, read them all. One at a time, each costs
# SEARCH_WAIT_SECONDS, so a pass over ~60 tracks that need a new source took over half an
# hour, and downloads sat idle for all of it.
#
# But the Soulseek server punishes searching too fast: 12 at once, batch after batch
# (~0.5 searches/second sustained), got the account banned for 30 minutes ("too many
# operations at once"), which drops the connection and kills every queued download. One
# search per ~25s never did. So searches are posted SEARCH_GAP_SECONDS apart (~0.1/s):
# still ~2x faster than sequential, and nowhere near the limit.
SEARCH_BATCH = 8
SEARCH_GAP_SECONDS = 10


def _search_many(queries: list[str]) -> list[list[dict]]:
    out: list[list[dict]] = [[] for _ in queries]
    for start in range(0, len(queries), SEARCH_BATCH):
        tokens = []
        for k, q in enumerate(queries[start:start + SEARCH_BATCH]):
            if k:
                time.sleep(SEARCH_GAP_SECONDS)
            tokens.append(_api_post("/search", {"query": q, "mode": "global"})["token"])
        time.sleep(SEARCH_WAIT_SECONDS)
        for i, token in enumerate(tokens):
            out[start + i] = _fetch_all_results(token)
    return out


def is_connected() -> bool:
    """Is Nicotine+ logged in to the Soulseek server? (Unknown counts as yes: this only
    exists to stop retries being wasted while it's known to be offline.)"""
    try:
        return bool(_api_get("/status").get("connected", True))
    except Exception:
        return True


def wait_until_connected(on_progress=None, max_wait: float = 3600, poll: float = 30) -> bool:
    """Block while Nicotine+ is disconnected from Soulseek (a ban, a dropped connection).
    Every queued transfer reads "User logged off" then, and treating that as a dead
    source would burn a track's retries on nothing. False if it never came back."""
    deadline = time.time() + max_wait
    while not is_connected():
        if on_progress:
            on_progress("Nicotine+ is disconnected from Soulseek -- waiting for it to reconnect "
                        "(open Nicotine+ and reconnect if it doesn't on its own)")
        if time.time() >= deadline:
            return False
        time.sleep(poll)
    return True


def _search(query: str) -> list[dict]:
    return _search_many([query])[0]


def _fallback_query(artist_full: str, title_main: str) -> str | None:
    """A prefix search for a single long word ("Rollercoaster" -> "Roller"), which
    also finds files spelled "Roller Coaster". Only worth trying for those titles."""
    words = re.findall(r"[A-Za-z0-9]+", _query_title(title_main))
    if len(words) != 1 or len(words[0]) < 8:
        return None
    primary_artist = artist_full.split(",")[0].strip()
    return f"{primary_artist} {words[0][:max(5, round(len(words[0]) * 0.45))]}".strip()


def _has_title_match(items: list[dict], artist_full: str, title_main: str, title_full: str) -> bool:
    return any(is_exact_title_match(i.get("file_path") or "", artist_full, title_main)
               and passes_version_guard((i.get("file_path") or "").lower(), title_full) for i in items)


# Peers that keep failing us. One uploader can hold dozens of our requests "Queued" and
# never serve any of them (a real run had 91 of 113 queued requests on a single peer);
# each track would otherwise have to find that out for itself, ~5 minutes at a time. A
# peer that stalled or died on PEER_STRIKE_LIMIT different tracks, and never delivered
# one, is ranked below every other source for ALL tracks. Ranked below, not excluded:
# if it's the only source there is, slow beats nothing.
PEER_STRIKE_LIMIT = 3
_peer_strikes: dict[str, int] = {}
_peer_delivered: set[str] = set()


def _avoided_peers() -> set[str]:
    return {u for u, n in _peer_strikes.items() if n >= PEER_STRIKE_LIMIT and u not in _peer_delivered}


class SearchStats(NamedTuple):
    raw: int        # results the network returned
    matched: int    # ...of which are this exact title (right song, right version)
    blocked: int = 0  # ...of which Nicotine+ already holds as a failed/finished transfer (see below)
    capped: int = 0   # ...of which sit with a peer we already have MAX_ACTIVE_PER_PEER requests open with


# Being polite to uploaders. One peer once held 91 of our requests at the same time, which is
# exactly what makes a person's client look like a bot: several uploaders answered with
# "prove you're human" messages. At most this many of our requests are open with any one peer
# at a time; a track whose only source is a busy peer simply waits for a slot (and that wait
# is not counted as a failed attempt).
MAX_ACTIVE_PER_PEER = 3


def _active_requests_per_peer() -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        for d in get_downloads(active_only=True):
            counts[d.get("username")] = counts.get(d.get("username"), 0) + 1
    except Exception:
        pass
    return counts


# Nicotine+ keeps every transfer it has ever had, keyed by (user, file). Queueing a file
# it already has a record of does NOTHING -- the API answers "duplicate" and the old
# status stays. So a source whose earlier attempt ended "File not shared" (or was already
# downloaded) can never be retried by asking again; picking it just burns an attempt. These
# are skipped. "User logged off" is not here: Nicotine+ resumes those on its own when the
# user comes back.
UNRETRIABLE_STATUSES = {
    "File not shared.", "Finished", "Cancelled", "Banned", "Banned (banana)",
    "Verification required", "Enqueue failed due to internal error",
}


def _unretriable_keys() -> set[tuple]:
    try:
        return {(d.get("username"), d.get("virtual_path") or d.get("file_path"))
                for d in get_downloads(active_only=False) if d.get("status") in UNRETRIABLE_STATUSES}
    except Exception:
        return set()


class SearchJob(NamedTuple):
    artist: str
    title: str
    mode: str                       # 'lossless' (FLAC, WAV or AIFF) or 'mp3' (a genuine 320kbps fallback)
    exclude: set                    # uploaders already tried for this track
    avoid: set                      # uploaders that keep failing us: usable, but ranked last


def find_candidates(jobs: list[SearchJob]) -> list[tuple[dict | None, SearchStats]]:
    """One search attempt per job, all run concurrently. Returns (best candidate or None,
    SearchStats) for each. The stats tell apart the three ways a search comes up empty: the
    network returned nothing (bad luck), it returned results but none was this song
    (usually our matching is too strict), or right-song files exist but none is usable
    (wrong format, dead peers)."""
    items = _search_many([_build_query(j.artist, j.title) for j in jobs])

    # a title spelled as one word here may be two words in people's filenames
    retry, retry_queries = [], []
    for i, j in enumerate(jobs):
        main = re.split(r"\s+-\s+", j.title, maxsplit=1)[0]
        fallback = _fallback_query(j.artist, main)
        if fallback and not _has_title_match(items[i], j.artist, main, j.title):
            retry.append(i)
            retry_queries.append(fallback)
    if retry_queries:
        for i, extra in zip(retry, _search_many(retry_queries)):
            items[i] = items[i] + extra

    blocked = _unretriable_keys()
    busy = _active_requests_per_peer()
    return [_pick(j, items[i], blocked, busy) for i, j in enumerate(jobs)]


def find_candidate(artist_full: str, title_full: str, mode: str, exclude_users: set[str],
                   avoid_users: set[str] | None = None) -> tuple[dict | None, SearchStats]:
    """A single search attempt (see find_candidates)."""
    return find_candidates([SearchJob(artist_full, title_full, mode, exclude_users, avoid_users or set())])[0]


def _pick(job: SearchJob, items: list[dict], blocked: set, busy: dict) -> tuple[dict | None, SearchStats]:
    artist_full, title_full, mode = job.artist, job.title, job.mode
    title_main = re.split(r"\s+-\s+", title_full, maxsplit=1)[0]
    cands = []
    matched = blocked_count = capped_count = 0
    for it in items:
        fp = it.get("file_path") or ""
        if not is_exact_title_match(fp, artist_full, title_main):
            continue
        if not passes_version_guard(fp.lower(), title_full):
            continue
        matched += 1
        if it.get("username") in job.exclude:
            continue
        if (it.get("username"), fp) in blocked:
            blocked_count += 1
            continue
        if busy.get(it.get("username"), 0) >= MAX_ACTIVE_PER_PEER:
            capped_count += 1
            continue
        attrs = it.get("file_attributes") or {}
        ranked = _lossless_candidate(it)
        if ranked is None and mode == "mp3" and _ext(fp) == ".mp3" and attrs.get("0") == 320:
            ranked = (FORMAT_TIER[".mp3"], 0)       # the fallback: lossless still wins if any turns up
        if ranked is None:
            continue
        cands.append((it, ranked))

    # Format tier first (FLAC > WAV = AIFF > MP3), then within a tier: a free upload
    # slot (no slot = it may never start), how much evidence the peer gives, speed, size.
    # A source that works beats a better format from a peer that keeps failing us.
    cands.sort(key=lambda c: (c[0].get("username") not in job.avoid, c[1][0], bool(c[0].get("free_upload_slots")),
                              c[1][1], c[0].get("upload_speed") or 0, c[0].get("size") or 0), reverse=True)
    best = cands[0][0] if cands else None
    if best is not None:
        busy[best.get("username")] = busy.get(best.get("username"), 0) + 1     # counts for the next job in this batch
    return best, SearchStats(len(items), matched, blocked_count, capped_count)


# ─── Peers asking "are you a bot?" ──────────────────────────────────────────
# Some uploaders answer a download request with a private message such as
#   ProveIt: To prove you are a human downloading these files, please type "X" in this chat
# and only serve people who reply. sff does NOT answer these itself: they exist to tell a
# person from a program, and a program answering defeats them. It only reads Nicotine+'s
# private-chat logs and tells you who asked and what to type, so you can reply yourself.
_VERIFY_LINE = re.compile(r"^(?P<when>\d+/\d+/\d+ \d+:\d+:\d+ [AP]M) \[(?P<user>[^\]]+)\] (?P<text>.*(?:prove|human|whitelist|robot|bot\b).*)$",
                          re.IGNORECASE)


def find_verification_requests(since: float, logs_dir: str | Path | None = None) -> list[dict]:
    """[{user, text, phrase}] for verification messages received after `since` (epoch seconds),
    one per user (the most recent)."""
    from datetime import datetime
    folder = Path(logs_dir) if logs_dir else Path(os.environ.get("APPDATA", "")) / "nicotine" / "logs" / "private"
    latest: dict[str, dict] = {}
    try:
        files = list(folder.glob("*.log"))
    except OSError:
        return []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        except OSError:
            continue
        for line in lines:
            m = _VERIFY_LINE.match(line.strip())
            if not m or m["user"].lower() == "server":
                continue
            try:
                when = datetime.strptime(m["when"], "%m/%d/%Y %I:%M:%S %p").timestamp()
            except ValueError:
                continue
            if when < since:
                continue
            quoted = re.search(r'"([^"]{1,60})"', m["text"])
            latest[m["user"]] = {"user": m["user"], "text": m["text"], "phrase": quoted.group(1) if quoted else None,
                                 "when": when}
    return sorted(latest.values(), key=lambda r: r["when"])


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
                 "max_raw", "max_matched", "max_blocked")

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
        self.max_blocked = 0

    def note_search(self, stats: SearchStats) -> None:
        self.max_raw = max(self.max_raw, stats.raw)
        self.max_matched = max(self.max_matched, stats.matched)
        self.max_blocked = max(self.max_blocked, stats.blocked)

    def why_no_source(self) -> str:
        if self.max_raw == 0:
            return "the network returned no results for it"
        if self.max_matched == 0:
            return f"{self.max_raw} results came back but none matched this title"
        note = f", {self.max_blocked} of them already failed or were used up in Nicotine+" if self.max_blocked else ""
        return f"{self.max_matched} matching files were found but none was usable{note}"


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
    to_search: list[tuple] = []
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

        to_search.append((track, st))

    for start in range(0, len(to_search), SEARCH_BATCH):
        chunk = to_search[start:start + SEARCH_BATCH]
        if on_progress:
            on_progress(f"Searching Soulseek ({start + len(chunk)}/{len(to_search)}): "
                        f"{chunk[0][0].artist} - {chunk[0][0].title} and {len(chunk) - 1} more")
        wait_until_connected(on_progress)
        avoid = _avoided_peers()
        for (track, st), (best, stats) in zip(chunk, find_candidates(
                [SearchJob(t.artist, t.title, "lossless", set(), avoid) for t, _ in chunk])):
            st.note_search(stats)
            if best:
                enqueue(best)
                st.key = (best["username"], best["file_path"])
            logger.info("search 1: %s - %s -> %s (%d results, %d matching)", track.artist, track.title,
                        f'{best["username"]}: {best["file_path"][-50:]}' if best else "nothing usable", stats.raw, stats.matched)
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
                download_dir: str | None = None, validate=None, on_notice=None) -> None:
    """Poll until every track is downloaded or given up: replaces stalled, dead or
    unusable sources with a freshly verified alternative (excluding every uploader
    already tried), falling back to a 320kbps MP3 only after several failed
    lossless attempts. Mutates `states` in place.

    `validate(path) -> bool` (with `download_dir`) is the "is this finished file
    actually usable" gate: a WAV that turns out to be compressed, or not audio at
    all, is treated like a dead source instead of being handed on."""
    start = time.time()
    told: set[str] = set()

    while True:
        wait_until_connected(on_progress)
        if on_notice:
            for req in find_verification_requests(since=start):
                if req["user"] not in told:
                    told.add(req["user"])
                    on_notice(req)
        elapsed = time.time() - start
        by_key = {}
        for d in get_downloads(active_only=False):
            key = (d.get("username"), d.get("virtual_path") or d.get("file_path"))
            by_key[key] = d

        replacements: list[_TrackState] = []
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
                    _peer_delivered.add(st.key[0])
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
                logger.info("replacing %s for %s - %s: status=%s", st.key[0], st.track.artist, st.track.title, status)
                st.tried_users.add(st.key[0])
                if not (status == "Finished"):          # it stalled or died, as opposed to sending junk
                    _peer_strikes[st.key[0]] = _peer_strikes.get(st.key[0], 0) + 1
            st.attempts += 1
            # Four searches (each also retried with a prefix query) and not one file of this
            # song has ever shown up: more of the same, 90 seconds apart, only wastes time.
            nothing_exists = st.key is None and st.attempts >= GIVE_UP_IF_NOTHING_AFTER and st.max_matched == 0
            if st.attempts > MAX_ATTEMPTS_PER_TRACK or nothing_exists:
                st.resolved = True
                logger.info("gave up on %s - %s after %d attempts: %s", st.track.artist, st.track.title,
                            st.attempts, st.why_no_source())
                if on_progress:
                    on_progress(f"Gave up: {st.track.artist} - {st.track.title} ({st.why_no_source()})")
                continue

            mode = st.mode
            if mode == "lossless" and st.attempts > LOSSLESS_ATTEMPTS_BEFORE_MP3_FALLBACK:
                mode = "mp3"
            st.mode = mode
            replacements.append(st)

        if replacements:
            if on_progress:
                on_progress(f"Finding new sources for {len(replacements)} tracks")
            avoid = _avoided_peers()
            found = find_candidates([SearchJob(st.track.artist, st.track.title, st.mode, set(st.tried_users), avoid)
                                     for st in replacements])
            for st, (best, stats) in zip(replacements, found):
                st.note_search(stats)
                if not best and stats.capped:
                    st.attempts -= 1        # only busy peers had it: waiting for a slot, not a failure
                if best:
                    enqueue(best)
                    st.key = (best["username"], best["file_path"])
                    st.history = []
                logger.info("attempt %d (%s): %s - %s -> %s (%d results, %d matching)", st.attempts, st.mode,
                            st.track.artist, st.track.title,
                            f'{best["username"]}: {best["file_path"][-50:]}' if best else "nothing usable",
                            stats.raw, stats.matched)

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
