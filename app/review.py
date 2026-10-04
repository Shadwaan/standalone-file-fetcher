"""A page for listening to two versions of a track and saying which one is right.

The audit of finished downloads (suspect_downloads_audit.csv) flags files that may be the wrong song,
a vocals-only stem, or a different version. A similarity score can't settle the grey area, so this
shows each flagged file next to the copy you already had (the YouTube MP3) and lets you listen to
both and mark the result. Marks are kept in review_marks.json; nothing here changes your library.
"""
import csv
import hashlib
import json
import logging
import re
import subprocess
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
AUDIT_FILE = APP_DIR / "suspect_downloads_audit.csv"
MARKS_FILE = APP_DIR / "review_marks.json"
CACHE_DIR = APP_DIR / ".review_cache"
STATE_FILE = APP_DIR / "sync_state.json"
PAGE = APP_DIR / "frontend" / "review.html"

MARKS = {
    "new_right": "The new file is the right song",
    "youtube_right": "The YouTube copy is right (the new file is wrong)",
    "both_ok": "Both fine (just a different version)",
    "neither": "Neither is right",
}

router = APIRouter()
_lock = threading.Lock()


def _item_id(new_file: str) -> str:
    return hashlib.md5(new_file.lower().encode("utf-8")).hexdigest()[:12]


def _artists_by_file() -> dict[str, str]:
    """new file path -> artist, from the sync state (the audit only recorded titles)."""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for pl in state.get("playlists", {}).values():
        for variant in (pl.get("variants") or {}).values():
            for rec in (variant.get("tracks") or {}).values():
                out[str(rec.get("file_path", "")).replace("\\", "/").lower()] = rec.get("artist", "")
    return out


def _group(row: dict) -> str:
    verdict = row.get("verdict", "")
    try:
        sim = float(row["similarity_to_reference"])
    except (KeyError, ValueError, TypeError):
        sim = None
    if "VOCALS" in verdict:
        return "stem"
    if sim is None:
        return "other"
    if sim < 0.3:
        return "wrong_clear"
    if sim < 0.7:
        return "grey"
    return "version"


def load_items() -> list[dict]:
    try:
        with AUDIT_FILE.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return []
    marks = load_marks()
    artists = _artists_by_file()
    items = []
    for r in rows:
        new_file, ref = r.get("new_file", ""), r.get("reference", "")
        iid = _item_id(new_file)
        try:
            sim = float(r["similarity_to_reference"])
        except (KeyError, ValueError, TypeError):
            sim = None
        items.append({
            "id": iid, "playlist": r.get("playlist", ""), "title": r.get("title", ""),
            "artist": artists.get(new_file.replace("\\", "/").lower(), ""),
            "verdict": r.get("verdict", ""), "group": _group(r), "similarity": sim,
            "bass_share": r.get("bass_share") or None,
            "new_name": Path(new_file).name, "old_name": Path(ref).name if ref else "",
            "has_new": Path(new_file).is_file(), "has_old": bool(ref) and Path(ref).is_file(),
            "mark": (marks.get(iid) or {}).get("mark"),
        })
    return items


def load_marks() -> dict:
    try:
        return json.loads(MARKS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _paths(item_id: str) -> tuple[Path | None, Path | None]:
    """The two files of one flagged item. Only files named in the audit can ever be served."""
    with AUDIT_FILE.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if _item_id(r.get("new_file", "")) == item_id:
                ref = r.get("reference", "")
                return Path(r["new_file"]), (Path(ref) if ref else None)
    raise HTTPException(status_code=404, detail="Unknown item")


def _playable(path: Path) -> Path:
    """A browser-playable file for `path`. MP3s are served as they are; anything else (AIFF, which
    Chrome can't play) is converted once to a 192 kbps MP3 and cached."""
    if path.suffix.lower() in {".mp3", ".wav", ".flac", ".m4a"}:
        return path
    CACHE_DIR.mkdir(exist_ok=True)
    key = hashlib.md5(f"{path}|{path.stat().st_mtime_ns}".encode("utf-8")).hexdigest()
    out = CACHE_DIR / f"{key}.mp3"
    with _lock:
        if not out.exists():
            tmp = out.with_suffix(".part.mp3")
            result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-map", "0:a:0",
                                     "-c:a", "libmp3lame", "-b:a", "192k", str(tmp)], capture_output=True)
            if result.returncode != 0 or not tmp.exists():
                raise HTTPException(status_code=500, detail="Could not prepare this file for playback")
            tmp.replace(out)
    return out


class Mark(BaseModel):
    id: str
    mark: str | None = None       # one of MARKS, or null to clear


@router.get("/review")
def review_page():
    return FileResponse(str(PAGE), headers={"Cache-Control": "no-cache"})


@router.get("/api/review/items")
def review_items():
    items = load_items()
    counts: dict[str, int] = {}
    for it in items:
        counts[it["group"]] = counts.get(it["group"], 0) + 1
    return {"items": items, "marks": MARKS, "total": len(items),
            "marked": sum(1 for it in items if it["mark"]), "groups": counts}


@router.get("/api/review/audio/{item_id}/{which}")
def review_audio(item_id: str, which: str):
    if which not in ("new", "old"):
        raise HTTPException(status_code=404, detail="Unknown file")
    new, old = _paths(item_id)
    path = new if which == "new" else old
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="That file is missing")
    playable = _playable(path)
    return FileResponse(str(playable), media_type="audio/mpeg" if playable.suffix == ".mp3" else None)


@router.post("/api/review/mark")
def review_mark(body: Mark):
    if body.mark is not None and body.mark not in MARKS:
        raise HTTPException(status_code=400, detail="Unknown mark")
    if not re.fullmatch(r"[0-9a-f]{12}", body.id):
        raise HTTPException(status_code=400, detail="Bad id")
    with _lock:
        marks = load_marks()
        if body.mark is None:
            marks.pop(body.id, None)
        else:
            marks[body.id] = {"mark": body.mark}
        MARKS_FILE.write_text(json.dumps(marks, indent=1), encoding="utf-8")
    return JSONResponse({"ok": True})
