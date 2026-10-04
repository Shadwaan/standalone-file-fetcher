"""Move your sff library to another computer without downloading anything.

Everything you chose lives in places a copied music folder does not carry: which playlist a track is
in and in what order, the version labels and kept songs, which labelled mixes are stand-ins that sff
should keep looking for. This writes all of it into one file (the manifest) next to the music, and reads
it back on the other computer to build the same Rekordbox playlists and sff's own records from the files
that are already there.

  On the PC (Rekordbox closed or open: this only reads):
      python manifest.py export --music-root D:/Music/Incoming --out E:/Music/Incoming/sff_manifest.json

  On the Mac (Rekordbox CLOSED; the first run is a dry run that only prints the plan):
      python manifest.py import --manifest /Volumes/SSD/Music/Incoming/sff_manifest.json \\
                                --music-root /Volumes/SSD/Music/Incoming
      ...then add --apply to do it. Rekordbox's database is backed up first.

Nothing is downloaded, and nothing is deleted or overwritten: re-running is safe.
"""
import argparse
import json
import logging
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

logger = logging.getLogger("manifest")
FORMAT_VERSION = 1


def _posix(path) -> str:
    return str(path).replace("\\", "/")


def _relative(path: str, root: str) -> str | None:
    p, r = _posix(path), _posix(root).rstrip("/") + "/"
    return p[len(r):] if p.lower().startswith(r.lower()) else None


# ----------------------------------------------------------------------------------------- export
def build_manifest(state: dict, music_root: str, rb) -> dict:
    """The manifest for every lossless playlist sff made. `rb` is the rekordbox service (or a stand-in)."""
    by_path = {}                                    # file path -> what the sync state knows about it
    for pl_id, pl in state.get("playlists", {}).items():
        for fmt, v in (pl.get("variants") or {}).items():
            for sid, rec in (v.get("tracks") or {}).items():
                by_path[_posix(rec.get("file_path", "")).lower()] = (sid, rec)

    playlists, outside = [], []
    for pl_id, pl in state.get("playlists", {}).items():
        for fmt, v in (pl.get("variants") or {}).items():
            rb_id = v.get("rb_playlist_id") or rb.find_playlist_id(v.get("display_name", ""))
            if not rb_id:
                continue
            tracks = []
            for entry in rb.get_playlist_entries(rb_id):
                rel = _relative(entry["path"], music_root)
                if rel is None:
                    outside.append((v.get("display_name"), entry["title"], entry["path"]))
                    continue
                sid, rec = by_path.get(_posix(entry["path"]).lower(), (None, {}))
                tracks.append({
                    "position": len(tracks) + 1, "title": entry["title"], "artist": entry["artist"], "album": entry["album"],
                    "year": entry["year"], "path": rel, "spotify_id": sid,
                    "stand_in": bool(rec.get("stand_in")), "stand_in_label": rec.get("stand_in_label", ""),
                    "source": rec.get("source"),
                })
            playlists.append({"name": v.get("display_name"), "format": fmt, "spotify_playlist_id": pl_id,
                              "spotify_name": pl.get("name", ""), "base_name": pl.get("display_name", ""), "tracks": tracks})
    return {"format": FORMAT_VERSION, "exported_at": datetime.now().isoformat(timespec="seconds"),
            "source_music_root": _posix(music_root), "playlists": playlists,
            "not_exported_outside_music_root": [{"playlist": a, "title": b, "path": c} for a, b, c in outside]}


def cmd_export(args) -> int:
    from services import rekordbox as rb
    state_file = Path(args.state) if args.state else APP_DIR / "sync_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    manifest = build_manifest(state, args.music_root, rb)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    n = sum(len(p["tracks"]) for p in manifest["playlists"])
    print(f"Wrote {out}: {len(manifest['playlists'])} playlists, {n} tracks.")
    for item in manifest["not_exported_outside_music_root"]:
        print(f"  NOT included (its file is outside the music folder): {item['playlist']} | {item['title']}")
    return 0


# ----------------------------------------------------------------------------------------- import
def plan_import(manifest: dict, music_root: str) -> dict:
    """What an import would do, without doing it: the files found, and the ones that are not."""
    root = Path(music_root)
    found, missing = [], []
    for pl in manifest["playlists"]:
        for t in pl["tracks"]:
            path = root / t["path"]
            (found if path.is_file() else missing).append((pl["name"], t, path))
    return {"found": found, "missing": missing}


def backup_rekordbox(backup_dir: Path) -> Path | None:
    from services.platform_paths import REKORDBOX_MASTER_DB
    db = Path(REKORDBOX_MASTER_DB)
    if not db.exists():
        return None
    dest = backup_dir / f"master.db.backup.{time.strftime('%Y%m%d_%H%M%S')}"
    dest.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db) + suffix)
        if f.exists():
            shutil.copy2(f, dest / f.name)
    return dest


def _audio_length_ms(path: Path) -> int:
    try:
        import mutagen
        return int(mutagen.File(str(path)).info.length * 1000)
    except Exception:
        return 0


def apply_import(manifest: dict, music_root: str, rb, state: dict, say=print) -> dict:
    """Build the Rekordbox playlists and seed sff's records. Returns counts."""
    from models.track import TrackInfo
    root = Path(music_root)
    done = {"playlists": 0, "tracks": 0, "missing": 0}
    for pl in manifest["playlists"]:
        pid = rb.find_or_create_playlist(pl["name"])
        if not pid:
            say(f"  could not create the Rekordbox playlist {pl['name']!r}; skipped")
            continue
        sp = state.setdefault("playlists", {}).setdefault(pl["spotify_playlist_id"], {
            "tracks": {}, "snapshot_id": "", "name": pl["spotify_name"], "display_name": pl["base_name"]})
        variant = sp.setdefault("variants", {}).setdefault(pl["format"], {"tracks": {}})
        variant["rb_playlist_id"], variant["display_name"] = pid, pl["name"]
        variant.setdefault("created_at", datetime.now().isoformat())
        say(f"{pl['name']}: {len(pl['tracks'])} tracks")
        for t in pl["tracks"]:
            path = root / t["path"]
            if not path.is_file():
                done["missing"] += 1
                say(f"    MISSING, skipped: {t['path']}")
                continue
            info = TrackInfo(spotify_id=t.get("spotify_id") or "", title=t["title"], artist=t["artist"], album=t.get("album") or "",
                             year=str(t.get("year") or ""), duration_ms=_audio_length_ms(path), artwork_url=None,
                             playlist_name=pl["name"], position=t["position"] - 1, file_extension=path.suffix.lstrip(".").lower())
            content_id = rb.import_track_unanalyzed(_posix(path), info).get("id")
            if not content_id or not rb.add_track_to_playlist(pid, content_id, t["position"]):
                done["missing"] += 1
                say(f"    could not add to Rekordbox: {t['title']}")
                continue
            done["tracks"] += 1
            if t.get("spotify_id"):                                  # sff's own record, with this computer's paths
                rec = {"filename": path.name, "file_path": _posix(path), "artist": t["artist"], "title": t["title"]}
                if t.get("stand_in"):
                    rec["stand_in"], rec["stand_in_label"] = True, t.get("stand_in_label", "")
                if t.get("source"):
                    rec["source"] = t["source"]
                variant["tracks"][t["spotify_id"]] = rec
        done["playlists"] += 1
    return done


def cmd_import(args) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT_VERSION:
        print(f"This manifest is format {manifest.get('format')}; this sff reads format {FORMAT_VERSION}.")
        return 2
    plan = plan_import(manifest, args.music_root)
    total = len(plan["found"]) + len(plan["missing"])
    print(f"{len(manifest['playlists'])} playlists, {total} tracks: {len(plan['found'])} files found under {args.music_root}, "
          f"{len(plan['missing'])} missing.")
    for name, t, path in plan["missing"][:15]:
        print(f"  missing: {name} | {t['path']}")
    if not args.apply:
        print("\nThis was a dry run. Close Rekordbox, then run it again with --apply to build the playlists.")
        return 0
    from services import rekordbox as rb
    from services.platform_paths import is_rekordbox_running
    if is_rekordbox_running():
        print("Rekordbox is open. Close it first: its library cannot be edited while it is running.")
        return 3
    backup = backup_rekordbox(Path(args.backup_dir))
    print(f"Rekordbox database backed up to: {backup}" if backup else "No Rekordbox database found to back up (a new one will be used).")
    state_file = Path(args.state) if args.state else APP_DIR / "sync_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {"playlists": {}, "last_sync": None, "failed": {}}
    if state_file.exists():
        shutil.copy2(state_file, state_file.with_name(state_file.name + f".bak.before_import_{time.strftime('%H%M%S')}"))
    done = apply_import(manifest, args.music_root, rb, state)
    rb.flush_wal()
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDone: {done['playlists']} playlists, {done['tracks']} tracks added, {done['missing']} skipped. "
          f"sff's records were written to {state_file}.")
    return 0 if not done["missing"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", help="write the manifest (run on the computer that has the playlists)")
    e.add_argument("--music-root", required=True, help="the music folder, e.g. D:/Music/Incoming")
    e.add_argument("--out", required=True, help="where to write the manifest, e.g. on the SSD next to the music")
    e.add_argument("--state", help="sync_state.json (default: the one in this folder)")
    i = sub.add_parser("import", help="build the playlists from the manifest and the files (run on the other computer)")
    i.add_argument("--manifest", required=True)
    i.add_argument("--music-root", required=True, help="where the music folder is on THIS computer")
    i.add_argument("--apply", action="store_true", help="actually do it (without this it only prints the plan)")
    i.add_argument("--state", help="sync_state.json to write (default: the one in this folder)")
    i.add_argument("--backup-dir", default=str(Path.home() / "sff_rekordbox_backups"))
    args = parser.parse_args(argv)
    return cmd_export(args) if args.command == "export" else cmd_import(args)


if __name__ == "__main__":
    sys.exit(main())
