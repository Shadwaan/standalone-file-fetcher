"""Where each file came from: which Soulseek user sent it.

Appends one row per track to `download_sources.csv` as soon as the file is in hand (before
any Rekordbox import), so it stays useful even if a later step fails. Handy for seeing which
uploaders are reliable and where a doubtful file came from.
"""
import csv
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

FILE = Path(__file__).resolve().parent.parent / "download_sources.csv"
_COLUMNS = ["time", "playlist", "artist", "title", "from_user", "remote_path", "how"]


def record(rows: list[dict]) -> None:
    """rows: [{playlist, artist, title, from_user, remote_path, how}]"""
    if not rows:
        return
    try:
        new_file = not FILE.exists()
        with FILE.open("a", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(_COLUMNS)
            now = datetime.now().isoformat(timespec="seconds")
            for r in rows:
                w.writerow([now, r.get("playlist", ""), r.get("artist", ""), r.get("title", ""),
                            r.get("from_user", ""), r.get("remote_path", ""), r.get("how", "")])
    except OSError as e:
        logger.warning("Could not save the download-sources record: %s", e)
    write_summary()


def top_users(limit: int = 10) -> list[tuple[str, int]]:
    """Users that sent us the most files, from the record."""
    try:
        with FILE.open(newline="", encoding="utf-8-sig") as fh:
            return Counter(r["from_user"] for r in csv.DictReader(fh) if r.get("from_user")).most_common(limit)
    except OSError:
        return []


_FORMAT_SUFFIX = re.compile(r"\s+(AIFF|WAV|FLAC)$", re.IGNORECASE)


def write_summary() -> None:
    """`download_sources_by_playlist.csv`: for each playlist, which uploaders supplied the most
    tracks, with the folder one of them came from. Genre-focused uploaders stand out, which is
    where to look (Browse Files in Nicotine+) for more of the same."""
    try:
        with FILE.open(newline="", encoding="utf-8-sig") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("from_user") and r.get("how") == "downloaded"]
    except OSError:
        return
    # one count per (playlist, user, track): the same file in a WAV and an AIFF playlist is one delivery
    seen, counts, example = set(), {}, {}
    for r in rows:
        playlist = _FORMAT_SUFFIX.sub("", r["playlist"])
        ident = (playlist, r["from_user"], r["artist"].lower(), r["title"].lower())
        if ident in seen:
            continue
        seen.add(ident)
        key = (playlist, r["from_user"])
        counts[key] = counts.get(key, 0) + 1
        folder = re.split(r"[\\/]", r["remote_path"])[:-1]
        example.setdefault(key, "\\".join(folder[-2:]))
    try:
        with FILE.with_name("download_sources_by_playlist.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["playlist", "uploader", "tracks_supplied", "an_example_folder"])
            for (playlist, user), n in sorted(counts.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1])):
                w.writerow([playlist, user, n, example[(playlist, user)]])
    except OSError as e:
        logger.warning("Could not save the uploaders-by-playlist report: %s", e)
