"""
Library-wide version of fix_progressive_db.py.

Scans EVERY DjmdContent row and fixes two bugs:

  1. SampleRate mismatch — DB value doesn't match what the actual MP3 file
     reports. Updates DB to match file.
  2. Broken ImagePath — path contains '/PIONEER/Artwork///' (a sentinel from
     tracks with AlbumID=None). Cleared to NULL so Rekordbox stops showing
     wrong artwork from the collided path.

Does NOT delete tracks, modify files, or touch anything else.

Usage:
    python fix_all_db.py             # dry run (ok with Rekordbox open)
    python fix_all_db.py --apply     # execute (Rekordbox must be closed)
"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from mutagen import File as MutagenFile


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
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = db_dir / f"master.db.bak.{ts}"
    shutil.copy2(db_dir / "master.db", dst)
    print(f"Backup: {dst}")
    return dst


def actual_sample_rate(fp: Path) -> int | None:
    try:
        audio = MutagenFile(str(fp))
        if audio and hasattr(audio, "info") and hasattr(audio.info, "sample_rate"):
            return int(audio.info.sample_rate)
    except Exception:
        pass
    return None


def main():
    apply = "--apply" in sys.argv

    if apply and rekordbox_running():
        print("ERROR: Rekordbox is running. Close it (and agents) before --apply.")
        sys.exit(1)

    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    db = Rekordbox6Database()

    contents = db.session.query(tables.DjmdContent).all()
    print(f"Scanning {len(contents)} DjmdContent rows...\n")

    sr_changes: list[tuple[str, str, int, int]] = []  # (cid, title, old, new)
    img_changes: list[tuple[str, str, str]] = []     # (cid, title, old)
    missing_files = 0
    no_sr_info = 0

    for c in contents:
        cid = str(c.ID)
        title = (c.Title or "")[:50]
        fp_str = str(c.FolderPath or "")

        ip = getattr(c, "ImagePath", None) or ""
        if BROKEN_IMAGE_MARKER in ip:
            img_changes.append((cid, title, ip))

        if not fp_str:
            continue
        fp = Path(fp_str)
        if not fp.exists():
            missing_files += 1
            continue

        actual_sr = actual_sample_rate(fp)
        if actual_sr is None:
            no_sr_info += 1
            continue

        db_sr = c.SampleRate
        if db_sr and actual_sr != db_sr:
            # Only fix SampleRate on tracks that aren't fully analyzed yet —
            # avoid disturbing Rekordbox's cached analysis state on analyzed rows.
            if c.Analysed != 105:
                sr_changes.append((cid, title, db_sr, actual_sr))

    print(f"--- SampleRate mismatches ({len(sr_changes)}) ---")
    for cid, t, old, new in sr_changes[:40]:
        print(f"  {cid}  '{t}'  {old} -> {new}")
    if len(sr_changes) > 40:
        print(f"  ... and {len(sr_changes) - 40} more")

    print(f"\n--- Broken ImagePath to clear ({len(img_changes)}) ---")
    for cid, t, old in img_changes[:40]:
        print(f"  {cid}  '{t}'  was: {old}")
    if len(img_changes) > 40:
        print(f"  ... and {len(img_changes) - 40} more")

    print(f"\n--- Summary ---")
    print(f"  Tracks scanned: {len(contents)}")
    print(f"  Missing files on disk: {missing_files} (skipped)")
    print(f"  Tracks without readable SR: {no_sr_info} (skipped)")
    print(f"  SR fixes: {len(sr_changes)}")
    print(f"  ImagePath fixes: {len(img_changes)}")

    if not sr_changes and not img_changes:
        print("\nNothing to change.")
        db.session.close()
        db.engine.dispose()
        return

    if not apply:
        print("\n(dry run — re-run with --apply to execute; Rekordbox must be closed)")
        db.session.close()
        db.engine.dispose()
        return

    db.session.close()
    db.engine.dispose()

    backup_db()

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

    print(f"\nApplied: {len(sr_changes)} SR + {len(img_changes)} ImagePath updates.")


if __name__ == "__main__":
    main()
