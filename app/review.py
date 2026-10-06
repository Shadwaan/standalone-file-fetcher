"""A page for listening to two versions of a track and saying which one is right.

The audit of finished downloads (suspect_downloads_audit.csv) flags files that may be the wrong song,
a vocals-only stem, or a different version. A similarity score can't settle the grey area, so this
shows each flagged file next to the real track (YouTube video, Spotify, or the older MP3). The
reference is always taken as right; the only question is whether the NEW file is. Marks are kept in
review_marks.json. A track can also carry a version label ("CZR's Peak Hour Mix") for a file that is a
different mix but kept on purpose; "Apply labels" writes it into the title in Rekordbox and in the file.
Nothing else here changes your library.
"""
import csv
import hashlib
from datetime import datetime
import json
import logging
import re
import subprocess
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from services.labels import with_label

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
from services import audit_log  # noqa: E402

AUDIT_FILE = audit_log.FILE
MARKS_FILE = APP_DIR / "review_marks.json"
HISTORY_FILE = APP_DIR / "review_history.jsonl"
CACHE_DIR = APP_DIR / ".review_cache"
STATE_FILE = APP_DIR / "sync_state.json"
PAGE = APP_DIR / "frontend" / "review.html"

# The reference is the truth, so a new file can only match it or not.
MARKS = {
    "right": "The new file matches the real track",
    "wrong": "The new file is wrong",
}

router = APIRouter()
_lock = threading.Lock()


def _item_id(new_file: str) -> str:
    return hashlib.md5(new_file.lower().encode("utf-8")).hexdigest()[:12]


def _tracks_by_file() -> dict[str, dict]:
    """new file path -> {artist, title, spotify_id}, from the sync state (the audit only recorded titles)."""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for pl in state.get("playlists", {}).values():
        for variant in (pl.get("variants") or {}).values():
            for spotify_id, rec in (variant.get("tracks") or {}).items():
                out[str(rec.get("file_path", "")).replace("\\", "/").lower()] = {
                    "artist": rec.get("artist", ""), "title": rec.get("title", ""), "spotify_id": spotify_id}
    return out


from services.suggest import suggest_label, suggest_other  # noqa: E402,F401  (shared with the sync)


def _group(row: dict) -> str:
    verdict = row.get("verdict", "")
    try:
        sim = float(row["similarity_to_reference"])
    except (KeyError, ValueError, TypeError):
        sim = None
    if "VOCALS" in verdict:
        return "stem"
    if sim is None:
        return "version" if "mix?" in verdict else "other"
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
    known = _tracks_by_file()
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
            "artist": known.get(new_file.replace("\\", "/").lower(), {}).get("artist", ""),
            "spotify_id": known.get(new_file.replace("\\", "/").lower(), {}).get("spotify_id", ""),
            "verdict": r.get("verdict", ""), "group": _group(r), "similarity": sim,
            "bass_share": r.get("bass_share") or None,
            "new_name": Path(new_file).name, "old_name": Path(ref).name if ref else "",
            "has_new": _where_now(new_file, marks.get(iid) or {}).is_file(), "has_old": bool(ref) and Path(ref).is_file(),
            "removed_from": (_where_now(new_file, marks.get(iid) or {}).parent.parent.name
                             if (marks.get(iid) or {}).get("rejected") else ""),
            "mark": (marks.get(iid) or {}).get("mark"),
            "label": (marks.get(iid) or {}).get("label", ""),
            "note": (marks.get(iid) or {}).get("note", ""),
            "applied_label": (marks.get(iid) or {}).get("applied_label", ""),
            "suggested_label": suggest_label(Path(new_file).stem, r.get("title", "")),
            "suggested_artist": suggest_other(Path(new_file).stem)[0],
            "suggested_title": suggest_other(Path(new_file).stem)[1],
            "keep_artist": ((marks.get(iid) or {}).get("keep") or {}).get("artist", ""),
            "keep_title": ((marks.get(iid) or {}).get("keep") or {}).get("title", ""),
            "keep_move": ((marks.get(iid) or {}).get("keep") or {}).get("move_to", ""),
            "rejected": bool((marks.get(iid) or {}).get("rejected")),
            "final": bool((marks.get(iid) or {}).get("final")),
            "standin_set": bool((marks.get(iid) or {}).get("standin_set")),
            "applied_keep": bool((marks.get(iid) or {}).get("keep")) and (marks.get(iid) or {}).get("applied_keep") == (marks.get(iid) or {}).get("keep"),
        })
    return items


# The first version of this page had four choices. The reference is always right, so they collapse to
# whether the NEW file is right or wrong; marks made under the old wording are converted, not lost.
_LEGACY_MARKS = {"new_right": "right", "both_ok": "right", "youtube_right": "wrong", "neither": "wrong"}


def pending_count() -> int:
    """Queued tracks you have not marked yet."""
    return sum(1 for item in load_items() if not item["mark"] and not item["keep_title"])


def load_marks() -> dict:
    try:
        marks = json.loads(MARKS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if any(m.get("mark") in _LEGACY_MARKS for m in marks.values()):
        for m in marks.values():
            m["mark"] = _LEGACY_MARKS.get(m.get("mark"), m.get("mark"))
        try:
            MARKS_FILE.write_text(json.dumps(marks, indent=1), encoding="utf-8")
        except OSError:
            pass
    return marks


def _where_now(new_file: str, entry: dict) -> Path:
    """The new file's current location: a file marked wrong and removed is in its playlist folder's _rejected."""
    path = Path(new_file)
    if entry.get("rejected"):
        recorded = entry.get("rejected_path")
        if recorded and Path(recorded).is_file():
            return Path(recorded)
        candidate = path.parent / "_rejected" / path.name
        if candidate.is_file():
            return candidate
    return path


def _paths(item_id: str) -> tuple[Path | None, Path | None]:
    """The two files of one flagged item. Only files named in the audit can ever be served."""
    marks = load_marks()
    with AUDIT_FILE.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if _item_id(r.get("new_file", "")) == item_id:
                ref = r.get("reference", "")
                return _where_now(r["new_file"], marks.get(item_id) or {}), (Path(ref) if ref else None)
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


_yt_cache: dict[str, list[dict]] = {}


def _youtube_search(query: str, limit: int = 5) -> list[dict]:
    """Top YouTube results for a query: id, title, channel, length. (Nothing is downloaded.)"""
    from yt_dlp import YoutubeDL
    with YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    return [{"id": e["id"], "title": e.get("title", ""), "channel": e.get("channel") or e.get("uploader") or "",
             "duration": e.get("duration"), "url": f"https://www.youtube.com/watch?v={e['id']}"}
            for e in (info.get("entries") or []) if e and e.get("id")]


class Mark(BaseModel):
    id: str
    mark: str | None = None       # one of MARKS, or null to clear


class Note(BaseModel):
    id: str
    label: str = ""               # version label that goes into the Rekordbox title
    note: str = ""                # private note, kept here only
    keep_title: str = ""          # a completely different song that is being kept: its new title...
    keep_artist: str = ""         # ...and artist (empty title = not keeping it as a different song)
    keep_move: str = ""           # optional: the playlist (e.g. "Dub Reggae Bass Addict AIFF") it belongs in instead
    final: bool = False           # a labelled mix that is the version wanted: stop looking for the Spotify one


# Set by main.py: the live SyncOrchestrator, whose in-memory state must be the one that is changed.
get_orchestrator = None


def _save_marks(marks: dict) -> None:
    MARKS_FILE.write_text(json.dumps(marks, indent=1, ensure_ascii=False), encoding="utf-8")


def _log_change(item_id: str, action: str, before: dict, after: dict) -> None:
    """Append what changed to review_history.jsonl, so a mark that looks wrong can be traced to what set it."""
    if before == after:
        return
    try:
        title = next((i["title"] for i in load_items() if i["id"] == item_id), "")
        with HISTORY_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "id": item_id, "title": title,
                                 "action": action, "before": before, "after": after}, ensure_ascii=False) + "\n")
    except OSError:
        pass


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


@router.get("/api/review/youtube/{item_id}")
def review_youtube(item_id: str):
    """The video(s) YouTube finds for this track, so the real thing can be watched and heard."""
    item = next((i for i in load_items() if i["id"] == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown item")
    if item_id not in _yt_cache:
        artist = item["artist"].split(",")[0].strip()
        try:
            _yt_cache[item_id] = _youtube_search(f"{artist} {item['title']}".strip())
        except Exception as e:                       # offline, YouTube changed, yt-dlp missing...
            logger.warning("YouTube search failed for %s: %s", item["title"], e)
            raise HTTPException(status_code=502, detail="Could not search YouTube right now")
    return {"query": f"{item['artist'].split(',')[0].strip()} {item['title']}", "results": _yt_cache[item_id]}


@router.get("/api/review/cover/{item_id}")
def review_cover(item_id: str):
    """The artwork embedded in the new file: Spotify's cover for the track it was meant to be."""
    new, _ = _paths(item_id)
    if not new.is_file():
        raise HTTPException(status_code=404, detail="That file is missing")
    from services import tagging
    picture = tagging.embedded_picture(new)
    if not picture:
        raise HTTPException(status_code=404, detail="No cover art in this file")
    return Response(content=picture, media_type="image/png" if picture[:4] == b"\x89PNG" else "image/jpeg",
                    headers={"Cache-Control": "max-age=3600"})


@router.post("/api/review/mark")
def review_mark(body: Mark):
    if body.mark is not None and body.mark not in MARKS:
        raise HTTPException(status_code=400, detail="Unknown mark")
    if not re.fullmatch(r"[0-9a-f]{12}", body.id):
        raise HTTPException(status_code=400, detail="Bad id")
    with _lock:
        marks = load_marks()
        entry = marks.get(body.id, {})
        before = json.loads(json.dumps(entry))
        entry.pop("mark_from_keep", None)       # a mark you set yourself is yours to keep
        if body.mark is None:
            entry.pop("mark", None)
        else:
            entry["mark"] = body.mark
        if entry:
            marks[body.id] = entry
        else:
            marks.pop(body.id, None)
        _save_marks(marks)
        _log_change(body.id, f"mark -> {body.mark}", before, entry)
    return JSONResponse({"ok": True})


@router.post("/api/review/note")
def review_note(body: Note):
    """Save a track's version label and private note (typed before or after marking it)."""
    if not re.fullmatch(r"[0-9a-f]{12}", body.id):
        raise HTTPException(status_code=400, detail="Bad id")
    with _lock:
        marks = load_marks()
        entry = marks.get(body.id, {})
        before = json.loads(json.dumps(entry))
        for key, value in (("label", " ".join(body.label.split())), ("note", body.note.strip())):
            if value:
                entry[key] = value
            else:
                entry.pop(key, None)
        if body.final:
            entry["final"] = True
        else:
            entry.pop("final", None)
        keep_title = " ".join(body.keep_title.split())
        if keep_title:
            entry["keep"] = {"title": keep_title, "artist": " ".join(body.keep_artist.split())}
            if body.keep_move.strip():
                entry["keep"]["move_to"] = body.keep_move.strip()
            if entry.get("mark") != "wrong":
                entry["mark"] = "wrong"         # it is not the Spotify track, so it is wrong for that
                entry["mark_from_keep"] = True  # remembered, so un-keeping can take it back
        else:
            if entry.pop("keep", None) is not None and entry.pop("mark_from_keep", False):
                entry.pop("mark", None)
        if entry:
            marks[body.id] = entry
        else:
            marks.pop(body.id, None)
        _save_marks(marks)
        _log_change(body.id, "note/label/keep saved", before, entry)
    return JSONResponse({"ok": True})


def _rekordbox_running() -> bool:
    from services.platform_paths import is_rekordbox_running
    return is_rekordbox_running()


def _current_playlist_of(path: Path) -> str:
    """The Rekordbox playlist (display name) the file is in, according to the sync state."""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    orchestrator = get_orchestrator() if get_orchestrator else None
    if orchestrator is not None:
        state = orchestrator._state
    target = path.as_posix().lower()
    for pl in state.get("playlists", {}).values():
        for variant in (pl.get("variants") or {}).values():
            for rec in (variant.get("tracks") or {}).values():
                if str(rec.get("file_path", "")).replace("\\", "/").lower() == target:
                    return variant.get("display_name", "")
    return ""


_last_rejected_to: dict[str, str] = {}


def _reject_file(path: Path, item: dict) -> str | None:
    """A file marked wrong: take it out of its Rekordbox playlist, put the file aside (never deleted) in
    the playlist folder's _rejected, point Rekordbox at the moved file so nothing reads as missing, forget
    its link to the Spotify track (so the next sync fetches the right one) and remember where it came from
    so that source is not picked again. Returns the playlist it was removed from, or None on failure."""
    from services import rejected
    from services import rekordbox as rb
    import shutil

    playlist = _current_playlist_of(path)
    if not playlist:
        return None
    source = _source_of(path)
    rb.remove_track_from_playlist(playlist, path.name)            # False when it was already out: that is fine
    aside = path.parent / "_rejected"
    aside.mkdir(exist_ok=True)
    from services.audio_formats import unique_path
    dest = unique_path(aside, path.stem, path.suffix.lstrip("."))
    shutil.move(str(path), str(dest))
    _last_rejected_to[path.as_posix()] = dest.as_posix()
    if not rb.update_content_path(path.as_posix(), dest.as_posix()):
        logger.warning("Rekordbox did not accept the moved path for %s; its entry may show as missing", path.name)
    _detach_from_spotify_track(path)
    if source:
        rejected.add(source.get("user", ""), source.get("remote_path", ""))
    return playlist


def _source_of(path: Path) -> dict:
    """Where a file came from, if sff recorded it (downloads since the source log existed)."""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    orchestrator = get_orchestrator() if get_orchestrator else None
    if orchestrator is not None:
        state = orchestrator._state
    target = path.as_posix().lower()
    for pl in state.get("playlists", {}).values():
        for variant in (pl.get("variants") or {}).values():
            for rec in (variant.get("tracks") or {}).values():
                if str(rec.get("file_path", "")).replace("\\", "/").lower() == target:
                    return rec.get("source") or {}
    return {}


def _music_folder() -> Path:
    from services import app_config
    return Path(app_config.get_music_folder())


def _move_to_playlist(path: Path, target_name: str) -> str | None:
    """Move a kept song to another playlist: the file into that playlist's folder, the track out of the
    playlist it is in and into the target one. It is NOT recorded against the target's Spotify list, so
    sync never mistakes it for a track that left Spotify and removes it. Returns the new path, or None."""
    from services import rekordbox as rb
    import shutil

    source_name, old_name = _current_playlist_of(path), path.name
    target_id = rb.find_or_create_playlist(target_name)
    if not target_id:
        return None
    dest_dir = _music_folder() / target_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    from services.audio_formats import unique_path
    dest = unique_path(dest_dir, path.stem, path.suffix.lstrip("."))
    shutil.move(str(path), str(dest))
    if not rb.update_content_path(path.as_posix(), dest.as_posix()):
        shutil.move(str(dest), str(path))                   # put the file back: Rekordbox must keep pointing at it
        return None
    content_id = rb.find_content_by_path(dest.as_posix())
    if source_name:
        rb.remove_track_from_playlist(source_name, old_name)
    already_there = rb.get_playlist_track_paths(target_id)
    if content_id and not rb.add_track_to_playlist(target_id, content_id, len(already_there) + 1):
        return None
    return dest.as_posix()


@router.get("/api/review/playlists")
def review_playlists():
    """The playlists a kept song can be moved to (those sff has made, by name)."""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"playlists": []}
    names = sorted({v.get("display_name") for pl in state.get("playlists", {}).values()
                    for v in (pl.get("variants") or {}).values() if v.get("rb_playlist_id") and v.get("display_name")})
    return {"playlists": names}


def _set_stand_in(path: Path, on: bool, label: str = "") -> bool:
    """Flag (or unflag) the sync-state record of a file as a stand-in for the Spotify mix."""
    orchestrator = get_orchestrator() if get_orchestrator else None
    if orchestrator is not None:
        state, save = orchestrator._state, orchestrator._save_state
    else:
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        save = lambda: STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")  # noqa: E731
    target = path.as_posix().lower()
    changed = False
    for pl in state.get("playlists", {}).values():
        for variant in (pl.get("variants") or {}).values():
            for rec in (variant.get("tracks") or {}).values():
                if str(rec.get("file_path", "")).replace("\\", "/").lower() == target:
                    if on:
                        rec["stand_in"], rec["stand_in_label"] = True, label
                    else:
                        rec.pop("stand_in", None)
                        rec.pop("stand_in_label", None)
                    changed = True
    if changed:
        save()
    return changed


def _detach_from_spotify_track(path: Path) -> None:
    """The file now holds a different song, so it no longer stands for the Spotify track it was
    downloaded for: forget that link, and the next sync sees that track as missing and fetches it."""
    orchestrator = get_orchestrator() if get_orchestrator else None
    if orchestrator is not None:
        state, save = orchestrator._state, orchestrator._save_state
    else:
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        save = lambda: STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")  # noqa: E731
    target = path.as_posix().lower()
    changed = False
    for pl in state.get("playlists", {}).values():
        for variant in (pl.get("variants") or {}).values():
            for sid, rec in list((variant.get("tracks") or {}).items()):
                if str(rec.get("file_path", "")).replace("\\", "/").lower() == target:
                    del variant["tracks"][sid]
                    changed = True
    if changed:
        save()


class Restore(BaseModel):
    id: str
    label: str = ""               # the mix name to show in the title, e.g. "other mix"


@router.post("/api/review/restore")
def review_restore(body: Restore):
    """Put a file that was marked wrong and removed back into its playlist, labelled as a different mix.
    It comes back as a stand-in: sff keeps looking for the mix Spotify actually lists."""
    if _rekordbox_running():
        raise HTTPException(status_code=409, detail="Close Rekordbox first: its library can't be edited while it is open.")
    if not re.fullmatch(r"[0-9a-f]{12}", body.id):
        raise HTTPException(status_code=400, detail="Bad id")
    from services import rekordbox as rb
    from services import tagging
    from services.audio_formats import unique_path
    import shutil
    label = " ".join(body.label.split())
    if not label:
        raise HTTPException(status_code=400, detail="Give the mix a name, so the playlist shows what it is.")
    with _lock:
        marks = load_marks()
        entry = marks.get(body.id) or {}
        if not entry.get("rejected"):
            raise HTTPException(status_code=400, detail="That file has not been removed.")
        item = next(i for i in load_items() if i["id"] == body.id)
        aside = _paths(body.id)[0]
        if not aside.is_file():
            raise HTTPException(status_code=404, detail="The set-aside file is missing.")
        home = aside.parent.parent                                   # the playlist's own folder
        playlist = home.name
        pid = rb.find_playlist_id(playlist)
        if not pid:
            raise HTTPException(status_code=404, detail=f"The Rekordbox playlist '{playlist}' was not found.")
        dest = unique_path(home, aside.stem, aside.suffix.lstrip("."))
        shutil.move(str(aside), str(dest))
        title = with_label(item["title"], label)
        if not rb.update_content_path(aside.as_posix(), dest.as_posix()):
            shutil.move(str(dest), str(aside))
            raise HTTPException(status_code=500, detail="Rekordbox did not accept the file's location; nothing was changed.")
        rb.set_title_by_path(dest.as_posix(), title)
        tagging.set_title(dest, title)
        content_id = rb.find_content_by_path(dest.as_posix())
        count = len(rb.get_playlist_track_paths(pid))
        if not (content_id and rb.add_track_to_playlist(pid, content_id, count + 1)):
            raise HTTPException(status_code=500, detail="The file was moved back but could not be added to the playlist.")
        for key in ("rejected", "rejected_path"):
            entry.pop(key, None)
        entry.update({"mark": "right", "label": label, "applied_label": label})
        entry["standin_set"] = False                      # the next sync records it as a stand-in (labelled entry, no plain one)
        marks[body.id] = entry
        _save_marks(marks)
    return {"restored": title, "playlist": playlist}


@router.post("/api/review/apply-labels")
def review_apply_labels():
    """Write each track's version label into its Rekordbox title and its file's title tag. A label that
    was applied and later removed puts the plain title back. Tracks marked wrong are left alone."""
    if _rekordbox_running():
        raise HTTPException(status_code=409, detail="Close Rekordbox first: its library can't be edited while it is open.")
    from services import rekordbox as rb
    from services import tagging
    applied, failed = [], []
    with _lock:
        marks = load_marks()
        for item in load_items():
            entry = marks.get(item["id"]) or {}
            path = _paths(item["id"])[0]
            keep = entry.get("keep")
            if keep:                                    # a different song, kept under its own name
                if entry.get("applied_keep") == keep:
                    continue
                if rb.set_title_by_path(path.as_posix(), keep["title"], keep.get("artist") or None):
                    tagging.set_title(path, keep["title"], keep.get("artist") or None)
                    moved_to = ""
                    if keep.get("move_to"):
                        moved_to = _move_to_playlist(path, keep["move_to"]) or ""
                        if not moved_to:
                            failed.append(item["title"] + " (renamed, but could not be moved to " + keep["move_to"] + ")")
                    _detach_from_spotify_track(path)
                    entry["applied_keep"] = keep
                    marks[item["id"]] = entry
                    now = (keep.get("artist") + " - " if keep.get("artist") else "") + keep["title"]
                    applied.append({"title": item["title"], "now": now + (f"  →  {keep['move_to']}" if moved_to else "")})
                else:
                    failed.append(item["title"])
                continue
            if entry.get("mark") == "wrong":                # a wrong file: out of its playlist, aside, and re-fetched
                if entry.get("rejected"):
                    continue
                outcome = _reject_file(path, item)
                if outcome:
                    entry["rejected"] = True
                    entry["rejected_path"] = _last_rejected_to.get(path.as_posix(), "")
                    marks[item["id"]] = entry
                    applied.append({"title": item["title"], "now": f"removed from {outcome}; the file is kept in _rejected"})
                else:
                    failed.append(item["title"] + " (could not be removed)")
                continue
            label, done = entry.get("label", ""), entry.get("applied_label", "")
            if label != done:
                title = with_label(item["title"], label)
                if rb.set_title_by_path(path.as_posix(), title):
                    tagging.set_title(path, title)
                    entry["applied_label"] = label
                    marks[item["id"]] = entry
                    applied.append({"title": item["title"], "now": title})
                else:
                    failed.append(item["title"])
                    continue
            # A labelled mix is a STAND-IN: sff keeps looking for the mix Spotify lists, unless this is the one wanted.
            wants_stand_in = bool(label) and not entry.get("final")
            if wants_stand_in != entry.get("standin_set", False):
                if _set_stand_in(path, wants_stand_in, label):
                    entry["standin_set"] = wants_stand_in
                    marks[item["id"]] = entry
                    applied.append({"title": item["title"],
                                    "now": "stand-in: sff keeps looking for the Spotify mix" if wants_stand_in
                                           else "final: sff stops looking for another version"})
        _save_marks(marks)
    return {"applied": applied, "failed": failed}
