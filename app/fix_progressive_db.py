"""
Fix two DB-side bugs on every track in the 'Progressive' playlist:

1. SampleRate: DB value corrected to match what the actual MP3 file reports.
2. ImagePath: clear any broken '/PIONEER/Artwork///' sentinel that collides
   across tracks (makes wrong artwork show). Setting to NULL makes Rekordbox
   display no artwork for those rows, which is correct when AlbumID is None.

Both fixes are reversible — master.db is backed up before --apply.

Usage:
    python fix_progressive_db.py             # dry run
    python fix_progressive_db.py --apply     # apply
"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from mutagen.mp3 import MP3


PLAYLIST_NAME = "Progressive"
BROKEN_IMAGE_MARKER = "/PIONEER/Artwork///"


def rekordbox_running() -> bool:
    import psutil
    for p in psutil.process_iter(["name"]):
        try:
            if "rekordbox" in (p.info["name"] or "").lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def backup_db() -> Path:
    db_dir = Path(os.environ["APPDATA"]) / "Pioneer" / "rekordbox"
    src = db_dir / "master.db"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = db_dir / f"master.db.bak.{ts}"
    shutil.copy2(src, dst)
    print(f"Backup: {dst}")
    return dst


def actual_sample_rate(fp: Path) -> int | None:
    try:
        return MP3(str(fp)).info.sample_rate
    except Exception:
        return None


def main():
    apply = "--apply" in sys.argv

    if apply and rekordbox_running():
        print("ERROR: Rekordbox is running. Close it before applying.")
        sys.exit(1)

    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    db = Rekordbox6Database()

    playlist = db.session.query(tables.DjmdPlaylist).filter_by(
        Name=PLAYLIST_NAME
    ).first()
    if not playlist:
        print(f"ERROR: Playlist '{PLAYLIST_NAME}' not found.")
        sys.exit(1)

    songs = (
        db.session.query(tables.DjmdSongPlaylist)
        .filter_by(PlaylistID=playlist.ID)
        .order_by(tables.DjmdSongPlaylist.TrackNo)
        .all()
    )

    sr_changes = []  # (content_id, title, old, new)
    img_changes = []  # (content_id, title, old)

    for song in songs:
        content = db.session.query(tables.DjmdContent).filter_by(
            ID=song.ContentID
        ).first()
        if not content:
            continue

        title = (content.Title or "")[:50]
        fp = Path(str(content.FolderPath or ""))

        # --- SampleRate check ---
        if fp.exists():
            actual_sr = actual_sample_rate(fp)
            db_sr = content.SampleRate
            if actual_sr and db_sr and actual_sr != db_sr:
                sr_changes.append((content.ID, title, db_sr, actual_sr))

        # --- ImagePath check ---
        ip = getattr(content, "ImagePath", None)
        if ip and BROKEN_IMAGE_MARKER in ip:
            img_changes.append((content.ID, title, ip))

    print(f"\n--- SampleRate mismatches ({len(sr_changes)}) ---")
    for cid, t, old, new in sr_changes:
        print(f"  CID={cid}  '{t}'  {old} -> {new}")

    print(f"\n--- Broken ImagePath to clear ({len(img_changes)}) ---")
    for cid, t, old in img_changes:
        print(f"  CID={cid}  '{t}'  was: {old}")

    if not sr_changes and not img_changes:
        print("\nNothing to change.")
        db.session.close()
        db.engine.dispose()
        return

    if not apply:
        print("\n(dry run — re-run with --apply to execute)")
        db.session.close()
        db.engine.dispose()
        return

    db.session.close()
    db.engine.dispose()

    backup_db()

    # Re-open for writes
    db2 = Rekordbox6Database()

    for cid, _t, _old, new in sr_changes:
        c = db2.session.query(tables.DjmdContent).filter_by(ID=cid).first()
        if c:
            c.SampleRate = new

    for cid, _t, _old in img_changes:
        c = db2.session.query(tables.DjmdContent).filter_by(ID=cid).first()
        if c:
            c.ImagePath = None

    db2.session.commit()

    from sqlalchemy import text
    db2.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    db2.session.commit()
    db2.session.close()
    db2.engine.dispose()

    print(f"\nApplied: {len(sr_changes)} SampleRate + {len(img_changes)} ImagePath updates.")
    print("Open Rekordbox to verify.")


if __name__ == "__main__":
    main()
