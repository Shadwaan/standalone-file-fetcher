"""Keep the review page's decisions in step between two computers (the PC and the Mac) through the manifest.

The review page remembers, per flagged file: your mark (right/wrong), the version label, the private note, the
"this is the one I want" tick, and a kept-different-song rename. Those live in review_marks.json, outside Rekordbox
and outside the music folder, so a copied library does not carry them. This writes them into the manifest (keyed by the
file's path INSIDE the music folder, which is the same on both computers) and reads them back on the other one.

Merging never overwrites: an item the other side has not seen is added; a field one side has not filled in is filled
from the other; where both sides hold different values the local one stays and the clash is reported.
"""
import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FIELDS = ("mark", "label", "note", "final", "keep", "applied_label", "applied_keep", "standin_set", "mark_from_keep")
AUDIT_COLUMNS = ("playlist", "title", "verdict", "similarity_to_reference", "bass_share")


def _posix(path) -> str:
    return str(path).replace("\\", "/")


def _relative(path: str, root: str) -> str | None:
    p, r = _posix(path), _posix(root).rstrip("/") + "/"
    return p[len(r):] if p.lower().startswith(r.lower()) else None


def _empty(value) -> bool:
    return value in ("", None, {})


def build_bundle(music_root: str) -> dict:
    """The review decisions for every flagged file that is inside `music_root` and still there."""
    import review as rv
    audit, marks_out, skipped = [], {}, 0
    try:
        with rv.AUDIT_FILE.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        rows = []
    marks = rv.load_marks()
    for row in rows:
        new_file = row.get("new_file", "")
        entry = marks.get(rv._item_id(new_file)) or {}
        rel = _relative(new_file, music_root)
        if entry.get("rejected") or rel is None or not rv._where_now(new_file, entry).is_file():
            skipped += 1                          # set aside, outside the music folder, or gone: nothing to review there
            continue
        audit.append({**{c: row.get(c, "") for c in AUDIT_COLUMNS}, "new_file": rel})
        kept = {k: entry[k] for k in FIELDS if k in entry and not _empty(entry[k])}
        if kept:
            marks_out[rel] = kept
    return {"audit": audit, "marks": marks_out, "skipped": skipped}


def apply_bundle(bundle: dict, music_root: str, write: bool = True) -> dict:
    """Merge a bundle into this computer's review queue and marks. Returns what happened."""
    import review as rv
    from services import audit_log
    root = Path(music_root)
    bundle = bundle or {}
    marks = rv.load_marks()
    added, filled, same, conflicts = [], [], 0, []
    queue = []
    for item in bundle.get("audit", []):
        path = root / item["new_file"]
        entry = (bundle.get("marks") or {}).get(item["new_file"]) or {}
        if rv._where_now(str(path), entry).is_file():
            queue.append({**item, "new_file": str(path), "reference": ""})
    for rel, incoming in (bundle.get("marks") or {}).items():
        iid = rv._item_id(str(root / rel))
        local = marks.get(iid)
        if local is None:
            marks[iid] = dict(incoming)
            added.append(rel)
            continue
        changed = False
        for key, value in incoming.items():
            if key not in local or _empty(local[key]):
                local[key] = value
                changed = True
            elif local[key] == value:
                same += 1
            else:
                conflicts.append({"file": rel, "field": key, "kept_here": local[key], "other": value})
        if changed:
            filled.append(rel)
    queued = audit_log.append(queue) if write else len(queue)
    if write and (added or filled):
        rv._save_marks(marks)
    return {"queued": queued, "marks_added": len(added), "marks_filled": len(filled), "unchanged_fields": same,
            "conflicts": conflicts}
