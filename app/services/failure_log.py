"""A record of the tracks Soulseek could not supply, that survives restarts.

Sync results live in memory and vanish when sff is turned off; this keeps them.
`failed_tracks.json` accumulates across runs (a track that fails again just gets its
counters bumped, one that later succeeds is removed) and `failed_tracks.csv` is the same
list in a form you can open in a spreadsheet or work through by hand.
"""
import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

FILE = Path(__file__).resolve().parent.parent / "failed_tracks.json"
CSV_FILE = FILE.with_suffix(".csv")

HINTS = {
    "no results": "Nothing on Soulseek from this account's vantage point (a closed port hides some peers). "
                  "Try again later, or look elsewhere.",
    "no match": "Results came back but none was this exact song/version. Check the search by hand: a "
                "differently spelled title may exist.",
    "wrong quality / dead source": "The song exists, but only as a lossy file below 320 kbps, or from sources that "
                                   "already failed (Nicotine+ remembers them; clear failed transfers in its "
                                   "Downloads tab to let sff retry).",
    "source did not deliver": "Sources were found but never sent the file (queued too long, file no longer "
                              "shared, or the user went offline). Worth retrying later.",
    "could not convert": "The file downloaded but could not be converted or tagged. Check sff.log.",
}


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _search_terms(artist: str, title: str) -> str:
    main = re.split(r"\s+-\s+", title, maxsplit=1)[0]
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", f"{artist.split(',')[0]} {main}")
    return re.sub(r"\s+", " ", text).strip()


def record(failures: list[dict], resolved_ids=()) -> None:
    """failures: [{spotify_id, artist, title, playlist, category, detail, attempts}].
    resolved_ids: tracks that have since been supplied, dropped from the record."""
    try:
        data = _load()
        now = datetime.now().isoformat(timespec="seconds")
        for sid in resolved_ids:
            data.pop(sid, None)
        for f in failures:
            old = data.get(f["spotify_id"], {})
            data[f["spotify_id"]] = {
                **f, "first_failed": old.get("first_failed", now), "last_failed": now,
                "times_failed": old.get("times_failed", 0) + 1,
            }
        FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        rows = sorted(data.values(), key=lambda r: (r.get("category", ""), r.get("playlist", ""), r.get("artist", "")))
        with CSV_FILE.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["playlist", "artist", "title", "category", "times_failed", "last_failed", "detail",
                        "what_to_do", "search_terms"])
            for r in rows:
                w.writerow([r.get("playlist", ""), r.get("artist", ""), r.get("title", ""), r.get("category", ""),
                            r.get("times_failed", 1), r.get("last_failed", ""), r.get("detail", ""),
                            HINTS.get(r.get("category", ""), ""), _search_terms(r.get("artist", ""), r.get("title", ""))])
    except OSError as e:
        logger.warning("Could not save the failed-tracks record: %s", e)
