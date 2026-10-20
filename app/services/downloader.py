"""
YouTube download + ID3 tagging service.

Downloads audio from YouTube via yt-dlp, converts to MP3 320kbps,
and tags with ID3 metadata from Spotify.
"""

import logging
import os
from pathlib import Path

import requests
import yt_dlp
from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1
from mutagen.mp3 import MP3

from models.track import TrackInfo
from services.platform_paths import FFMPEG_DIR, FFMPEG_PATH

logger = logging.getLogger(__name__)

# Duration tolerance for YouTube matching (seconds)
DURATION_TOLERANCE_SECS = 30

# Cap on candidate videos tried per track, across all search queries
MAX_DOWNLOAD_ATTEMPTS = 5

# Make ffmpeg discoverable to yt-dlp's child processes via PATH
if FFMPEG_DIR and FFMPEG_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")


def download_track(track: TrackInfo, output_dir: str) -> Path | None:
    """
    Download a track from YouTube, convert to MP3, and tag with Spotify metadata.
    Returns the path to the tagged MP3, or None on failure.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Try multiple search queries (shorter queries first for long titles)
    first_artist = track.artist.split(",")[0].strip()
    queries = [
        f"{track.artist} - {track.title}",
        f"{first_artist} - {track.title}",
        f"{first_artist} {track.title}",
        f"{track.title} {first_artist}",
        f"{track.title}",
    ]
    # Deduplicate while preserving order
    seen = set()
    queries = [q for q in queries if not (q in seen or seen.add(q))]

    # Try each query's candidates in ranked order. A candidate can be
    # unavailable (removed / region-blocked) even though search listed it —
    # extract_flat search doesn't check availability — so on failure fall
    # through to the next candidate instead of giving up on the track.
    tried: set[str] = set()
    for query in queries:
        logger.info("Searching YouTube for: %s", query)
        for video_url in _search_youtube(query, track.duration_ms):
            if video_url in tried:
                continue
            if len(tried) >= MAX_DOWNLOAD_ATTEMPTS:
                break
            tried.add(video_url)

            logger.info("Found YouTube match: %s", video_url)
            file_path = _download_audio(video_url, track, output_path)
            if file_path:
                _tag_file(file_path, track)
                return file_path
            logger.warning("Candidate failed, trying next: %s", video_url)

    if tried:
        logger.error("All %d YouTube candidates failed for: %s - %s", len(tried), track.artist, track.title)
    else:
        logger.error("No YouTube match found for: %s - %s (tried %d queries)", track.artist, track.title, len(queries))
    return None


def fetch_fallback(track: TrackInfo, output_dir: Path, accept=None, validate=None, max_candidates: int = 4) -> dict | None:
    """The best YouTube copy of `track` that passes the checks, for a track nothing else could supply.
    accept(candidate) -> bool judges a search result BEFORE it is downloaded (its title, length, uploader);
    validate(path) -> True, or a short reason, judges the downloaded file. Returns
    {"path", "url", "video_title", "uploader", "codec", "abr"} or None."""
    output_dir.mkdir(parents=True, exist_ok=True)
    first_artist = track.artist.split(",")[0].strip()
    queries = [f"{track.artist} - {track.title}", f"{first_artist} - {track.title}", f"{first_artist} {track.title}"]
    seen: set[str] = set()
    queries = [q for q in queries if not (q in seen or seen.add(q))]
    tried: set[str] = set()
    for query in queries:
        for cand in _search_youtube_entries(query, track.duration_ms, tolerance=20, only_in_tolerance=True):
            if cand["url"] in tried:
                continue
            if accept and not accept(cand):
                logger.info("YouTube candidate skipped (%s): %s", "does not fit", cand["title"])
                continue
            if len(tried) >= max_candidates:
                return None
            tried.add(cand["url"])
            path, quality = _download_audio_info(cand["url"], track, output_dir)
            if not path:
                continue
            verdict = validate(path) if validate else True
            if verdict is not True:
                logger.info("YouTube copy rejected (%s): %s", verdict, cand["title"])
                path.unlink(missing_ok=True)
                continue
            _tag_file(path, track)
            return {"path": path, "url": cand["url"], "video_title": cand["title"], "uploader": cand["uploader"],
                    "codec": quality.get("codec", ""), "abr": quality.get("abr")}
    return None


def _search_youtube(query: str, expected_duration_ms: int) -> list[str]:
    """Search YouTube and return candidate URLs, best duration match first.

    Results within DURATION_TOLERANCE_SECS come first, closest first. If none
    are within tolerance, the single closest result is returned anyway.
    """
    return [e["url"] for e in _search_youtube_entries(query, expected_duration_ms)]


def _search_youtube_entries(query: str, expected_duration_ms: int, tolerance: int = DURATION_TOLERANCE_SECS,
                            only_in_tolerance: bool = False) -> list[dict]:
    """Like _search_youtube, but each candidate is {"url", "title", "duration", "uploader"}. With no known duration
    (0) the results stay in YouTube's own relevance order. `only_in_tolerance` drops every candidate whose length is
    off instead of falling back to the closest one."""
    expected_secs = expected_duration_ms / 1000

    # Use extract_flat=True to avoid errors from unavailable videos in search results
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "default_search": "ytsearch5",
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            results = ydl.extract_info(f"ytsearch5:{query}", download=False)
    except Exception as e:
        logger.error("YouTube search failed: %s", e)
        return []

    if not results or "entries" not in results:
        return []

    scored = []
    for entry in results["entries"]:
        if not entry or entry.get("duration") is None:
            continue
        url = entry.get("webpage_url") or entry.get("url")
        if url:
            scored.append((abs(entry["duration"] - expected_secs) if expected_secs > 0 else 0,
                           {"url": url, "title": entry.get("title") or "", "duration": entry["duration"],
                            "uploader": entry.get("uploader") or entry.get("channel") or ""}))
    if expected_secs > 0:
        scored.sort(key=lambda x: x[0])
    else:
        return [c for _, c in scored]                       # no length to compare with: YouTube's own order

    in_tolerance = [c for diff, c in scored if diff <= tolerance]
    if in_tolerance:
        return in_tolerance
    if only_in_tolerance:
        return []

    if scored:
        logger.warning(
            "Best YouTube match has %.0fs duration difference (tolerance: %ds) — downloading anyway",
            scored[0][0], tolerance,
        )
        return [scored[0][1]]

    return []


def _download_audio(url: str, track: TrackInfo, output_dir: Path) -> Path | None:
    """Download audio from YouTube URL, convert to MP3 320kbps."""
    return _download_audio_info(url, track, output_dir)[0]


def source_quality(meta: dict) -> dict:
    """What YouTube actually served, from yt-dlp's metadata: {"codec": "opus"|"aac"|..., "abr": kbps or None}.
    The MP3 that comes out says 320, but it is only as good as this."""
    codec = (meta.get("acodec") or "").lower()
    codec = "aac" if codec.startswith("mp4a") else ("opus" if codec.startswith("opus") else (codec.split(".")[0] or "audio"))
    abr = meta.get("abr")
    return {"codec": codec, "abr": round(abr) if isinstance(abr, (int, float)) and abr > 0 else None}


def _download_audio_info(url: str, track: TrackInfo, output_dir: Path) -> tuple[Path | None, dict]:
    """_download_audio, plus the quality of the stream that was downloaded."""
    output_template = str(output_dir / track.safe_filename) + ".%(ext)s"

    # A previous candidate that failed mid-download leaves a .part file under
    # the same name; yt-dlp would resume it with THIS video's data and splice
    # two different uploads together. Start every attempt clean.
    for leftover in output_dir.iterdir():
        if leftover.name.startswith(f"{track.safe_filename}."):
            leftover.unlink(missing_ok=True)

    # Ensure ffmpeg is on PATH (WinGet install location)
    _ensure_ffmpeg_path()

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG_DIR,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
        "noplaylist": True,
    }

    quality: dict = {}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                quality = source_quality(ydl.extract_info(url, download=False) or {})
            except Exception as e:                       # the quality is a label, never a reason to fail the download
                logger.info("Could not read the stream quality of %s: %s", url, e)
            ydl.extract_info(url, download=True)

        # Find the output file
        expected_path = output_dir / f"{track.safe_filename}.mp3"
        if expected_path.exists():
            logger.info("Downloaded: %s", expected_path.name)
            return expected_path, quality

        # Fallback: find any audio file matching the name
        audio_extensions = {".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg"}
        candidates = [c for c in output_dir.glob(f"{track.safe_filename}.*")
                      if c.suffix.lower() in audio_extensions]
        if candidates:
            logger.info("Downloaded: %s", candidates[0].name)
            return candidates[0], quality

        logger.error("Download completed but output file not found for: %s", track.safe_filename)
        return None, quality

    except Exception as e:
        logger.error("yt-dlp download failed for %s: %s", url, e)
        return None, quality


def _tag_file(file_path: Path, track: TrackInfo) -> None:
    """Apply ID3 tags to a downloaded MP3 using Spotify metadata."""
    if file_path.suffix.lower() != ".mp3":
        return

    try:
        audio = MP3(file_path)
    except Exception as e:
        logger.error("Failed to open file for tagging: %s — %s", file_path.name, e)
        return

    try:
        audio.add_tags()
    except Exception:
        pass  # Tags already exist

    tags = audio.tags
    tags.add(TPE1(encoding=3, text=track.artist))
    tags.add(TIT2(encoding=3, text=track.title))
    tags.add(TALB(encoding=3, text=track.album))
    if track.year:
        tags.add(TDRC(encoding=3, text=track.year))

    # Fetch and embed artwork
    if track.artwork_url:
        try:
            resp = requests.get(track.artwork_url, timeout=15)
            resp.raise_for_status()
            tags.add(APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # Cover (front)
                desc="Cover",
                data=resp.content,
            ))
        except requests.RequestException as e:
            logger.warning("Failed to fetch artwork for %s: %s", track.title, e)

    audio.save()
    logger.info("Tagged: %s", file_path.name)


def _ensure_ffmpeg_path():
    """No-op — ffmpeg PATH is set at module load."""
    pass
