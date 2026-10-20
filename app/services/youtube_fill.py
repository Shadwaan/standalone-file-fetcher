"""A labelled YouTube copy for a track nothing else could supply.

When Soulseek has nothing for a track (not even a 320 kbps MP3) and the playlist has no file for it, sff can fall back
to YouTube so the playlist is not left with a hole. The copy is never passed off as the real thing:

- it is labelled in the title with what YouTube really served, e.g.  Title [YT opus 130k]. The MP3 that comes out
  claims 320 kbps, but YouTube never serves that: the stream is Opus or AAC at roughly 130-160 kbps and the MP3 is a
  re-encode of it, so the label states the source's own codec and bitrate (known from yt-dlp, not guessed);
- the label makes it a stand-in, so every later sync keeps looking on Soulseek for the real thing, and when a
  lossless copy arrives it replaces the YouTube one;
- it is queued on the review page so a wrong video can be caught by ear.

Candidates are judged by title before they are downloaded (no live/cover/slowed/stem versions, the title's own words
must be there, the length must fit), and the file afterwards (not a stem, and the same song as any copy already held).
"""
import logging
import re
from pathlib import Path

from services import downloader, soulseek, verify_audio
from services.labels import norm_title

logger = logging.getLogger(__name__)

_NOT_THE_TRACK = ("live", "cover", "karaoke", "nightcore", "slowed", "reverb", "sped up", "8d", "mashup", "reaction",
                  "tutorial", "full album", "dj set", "podcast", "bass boosted", "type beat", "remake", "tribute")
_STOP = {"the", "a", "an", "of", "and", "in", "on", "to", "for", "with", "feat", "ft", "featuring", "vs"}


def quality_label(codec: str, abr) -> str:
    """'YT opus 130k': what the copy really is."""
    parts = ["YT"]
    if codec:
        parts.append(codec)
    if abr:
        parts.append(f"{int(abr)}k")
    return " ".join(parts)


def _words(text: str) -> list[str]:
    return [w for w in norm_title(text).split() if len(w) > 1 and w not in _STOP]


def candidate_fits(candidate: dict, track) -> bool:
    """Judge a YouTube search result by its title and uploader, before downloading it."""
    title = (candidate.get("title") or "").lower()
    wanted = track.title.lower()
    if soulseek.has_stem_marker(title) and not soulseek.has_stem_marker(wanted):
        return False
    if any(bad in title and bad not in wanted for bad in _NOT_THE_TRACK):
        return False
    if not soulseek.passes_version_guard(title, track.title):
        return False
    haystack = set(_words(title + " " + (candidate.get("uploader") or "")))
    needed = _words(soulseek._query_title(track.title))
    missing = [w for w in needed if w not in haystack]
    if missing and (len(needed) < 5 or len(missing) > 1):
        return False                                           # the song's own words must be in the video's title
    artist_words = _words(track.artist.split(",")[0])
    return not artist_words or any(w in haystack for w in artist_words)


def fill_in(track, work_dir: Path, reference: Path | None = None) -> dict | None:
    """Download the best acceptable YouTube copy. Returns {"path", "url", "video_title", "codec", "abr", "label"} or None."""
    def validate(path: Path):
        if not soulseek.has_stem_marker(track.title) and verify_audio.looks_like_stem(path):
            return "a vocals-only stem"
        if reference and reference != path:
            alike = verify_audio.compare(path, reference)
            if alike is not None and alike < verify_audio.SAME_SONG_REJECT_BELOW:
                return f"a different song from the copy you already have (match {alike:.2f})"
        return True

    found = downloader.fetch_fallback(track, work_dir, accept=lambda c: candidate_fits(c, track), validate=validate)
    if not found:
        return None
    found["label"] = quality_label(found.get("codec", ""), found.get("abr"))
    logger.info("YouTube fill-in for %s - %s: %s [%s]", track.artist, track.title, found["video_title"], found["label"])
    return found
