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


def _search_youtube(query: str, expected_duration_ms: int) -> list[str]:
    """Search YouTube and return candidate URLs, best duration match first.

    Results within DURATION_TOLERANCE_SECS come first, closest first. If none
    are within tolerance, the single closest result is returned anyway.
    """
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
            scored.append((abs(entry["duration"] - expected_secs), url))
    scored.sort(key=lambda x: x[0])

    in_tolerance = [url for diff, url in scored if diff <= DURATION_TOLERANCE_SECS]
    if in_tolerance:
        return in_tolerance

    if scored:
        logger.warning(
            "Best YouTube match has %.0fs duration difference (tolerance: %ds) — downloading anyway",
            scored[0][0], DURATION_TOLERANCE_SECS,
        )
        return [scored[0][1]]

    return []


def _download_audio(url: str, track: TrackInfo, output_dir: Path) -> Path | None:
    """Download audio from YouTube URL, convert to MP3 320kbps."""
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

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        # Find the output file
        expected_path = output_dir / f"{track.safe_filename}.mp3"
        if expected_path.exists():
            logger.info("Downloaded: %s", expected_path.name)
            return expected_path

        # Fallback: find any audio file matching the name
        audio_extensions = {".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg"}
        candidates = [c for c in output_dir.glob(f"{track.safe_filename}.*")
                      if c.suffix.lower() in audio_extensions]
        if candidates:
            logger.info("Downloaded: %s", candidates[0].name)
            return candidates[0]

        logger.error("Download completed but output file not found for: %s", track.safe_filename)
        return None

    except Exception as e:
        logger.error("yt-dlp download failed for %s: %s", url, e)
        return None


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
