"""Track lists from text files (e.g. SoundCloud set lists) as a playlist source for the Soulseek pipeline.

A list file is named after the playlist and has one track per line, "0:04:30 Artist - Title (Mix)"; the time
stamp is optional. The tracks go through exactly the same stages as a Spotify playlist: dedupe against the
library, search, download, check, convert, tag, import, and the listen-and-mark queue."""

import hashlib
import re
from pathlib import Path

from models.track import TrackInfo

_TIME = re.compile(r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\s+")
_TRAILING_BRACKETS = re.compile(r"\s*\[([^\[\]]+)\]\s*$")


def playlist_name(path: Path) -> str:
    """'House at 3_.txt' -> 'House at 3': a '?' in a title can't be in a file name, so it shows up as '_'."""
    return path.stem.strip().rstrip("_?").strip() or path.stem


def parse_line(line: str) -> tuple[str, str] | None:
    text = _TIME.sub("", line).strip()
    if " - " not in text:
        return None
    artist, title = (part.strip() for part in text.split(" - ", 1))
    # sff reads a trailing [..] as "a different mix standing in", so the mix is written in round brackets here
    title = _TRAILING_BRACKETS.sub(lambda m: f" ({m.group(1)})", title).strip()
    return (artist, title) if artist and title else None


def read_tracks(path: Path) -> list[TrackInfo]:
    name = playlist_name(path)
    tracks: list[TrackInfo] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        parsed = parse_line(line)
        if not parsed:
            continue
        artist, title = parsed
        sid = "setlist:" + hashlib.sha1(f"{name}|{artist}|{title}".lower().encode("utf-8")).hexdigest()[:12]
        tracks.append(TrackInfo(spotify_id=sid, title=title, artist=artist, album="", year="", duration_ms=0,
                                artwork_url=None, playlist_name=name, position=len(tracks), file_extension="flac"))
    return tracks


class SetlistSource:
    """Stands in for SpotifyService: the same two calls the pipeline makes."""

    def __init__(self, folder: str):
        self.files = sorted(p for p in Path(folder).glob("*.txt") if p.is_file())
        if not self.files:
            raise FileNotFoundError(f"No .txt track lists in {folder}")
        self._by_id = {f"setlist:{p.stem}": p for p in self.files}

    def get_prefixed_playlists(self) -> list[dict]:
        return [{"id": f"setlist:{p.stem}", "name": p.stem, "display_name": playlist_name(p),
                 "snapshot_id": "", "track_count": len(read_tracks(p))} for p in self.files]

    def get_playlist_tracks(self, playlist_id: str, playlist_name_: str) -> list[TrackInfo]:
        return read_tracks(self._by_id[playlist_id])
