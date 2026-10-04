"""The queue of downloads that deserve a human ear.

After each sync, anything the checks could not settle on their own is appended here and shows up on the
review page: a file that only partly matches the copy you already had, or whose file name carries a version
the Spotify title does not mention ("(CZR's Peak Hour vocal mix)"). The checks are deterministic; you are
the judge. A file is queued once: marks you have made are kept by the review page, not here.
"""
import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FILE = Path(__file__).resolve().parent.parent / "suspect_downloads_audit.csv"
COLUMNS = ["playlist", "title", "verdict", "similarity_to_reference", "bass_share", "new_file", "reference"]


def append(rows: list[dict]) -> int:
    """Queue rows (skipping files already queued). Returns how many were added."""
    if not rows:
        return 0
    try:
        existing = set()
        if FILE.exists():
            with FILE.open(newline="", encoding="utf-8-sig") as fh:
                existing = {r.get("new_file", "").replace("\\", "/").lower() for r in csv.DictReader(fh)}
        fresh = [r for r in rows if str(r.get("new_file", "")).replace("\\", "/").lower() not in existing]
        if not fresh:
            return 0
        new_file = not FILE.exists()
        with FILE.open("a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            if new_file:
                w.writeheader()
            for r in fresh:
                w.writerow({c: r.get(c, "") for c in COLUMNS})
        return len(fresh)
    except OSError as e:
        logger.warning("Could not queue tracks for review: %s", e)
        return 0
