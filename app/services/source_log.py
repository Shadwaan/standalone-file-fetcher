"""Where each file came from: which Soulseek user sent it.

Appends one row per track to `download_sources.csv` as soon as the file is in hand (before
any Rekordbox import), so it stays useful even if a later step fails. Handy for seeing which
uploaders are reliable and where a doubtful file came from.
"""
import csv
import logging
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


def top_users(limit: int = 10) -> list[tuple[str, int]]:
    """Users that sent us the most files, from the record."""
    try:
        with FILE.open(newline="", encoding="utf-8-sig") as fh:
            return Counter(r["from_user"] for r in csv.DictReader(fh) if r.get("from_user")).most_common(limit)
    except OSError:
        return []
