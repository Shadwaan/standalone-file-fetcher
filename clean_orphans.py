"""
Delete orphan rows in master.db tables that reference DjmdContent IDs no
longer present. These accumulate from track deletions (mine, sff's, or
Rekordbox's own) where related-table cleanup didn't cascade.

Currently targets:
  DjmdCue, DjmdMixerParam, DjmdSongPlaylist, DjmdSongSampler

Rekordbox must be closed. master.db is backed up before --apply.

Usage:
    python clean_orphans.py             # dry run
    python clean_orphans.py --apply     # execute
"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


TARGET_TABLES = ["DjmdCue", "DjmdMixerParam", "DjmdSongPlaylist", "DjmdSongSampler"]


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


def main():
    apply = "--apply" in sys.argv

    if apply and rekordbox_running():
        print("ERROR: Rekordbox is running. Close it before --apply.")
        sys.exit(1)

    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    db = Rekordbox6Database()
    valid_ids = {str(c.ID) for c in db.session.query(tables.DjmdContent.ID).all()}

    plan: dict[str, list[int]] = {}  # table -> list of row IDs to delete
    for tname in TARGET_TABLES:
        T = getattr(tables, tname, None)
        if T is None or not hasattr(T, "ContentID"):
            continue
        orphan_pks = []
        for row in db.session.query(T).all():
            if str(row.ContentID) not in valid_ids:
                # Most tables have a primary key 'ID'; some may differ
                pk = getattr(row, "ID", None)
                orphan_pks.append(pk)
        plan[tname] = orphan_pks

    print("Orphan rows to delete:")
    grand_total = 0
    for tname, pks in plan.items():
        print(f"  {tname}: {len(pks)}")
        grand_total += len(pks)
    print(f"  Total: {grand_total}")

    if grand_total == 0:
        print("Nothing to clean.")
        return

    if not apply:
        print("\n(dry run — re-run with --apply to delete)")
        db.session.close()
        db.engine.dispose()
        return

    db.session.close()
    db.engine.dispose()

    backup_db()

    db2 = Rekordbox6Database()
    deleted = 0
    for tname, pks in plan.items():
        T = getattr(tables, tname)
        for pk in pks:
            row = db2.session.query(T).filter_by(ID=pk).first()
            if row:
                db2.session.delete(row)
                deleted += 1
    db2.session.commit()

    from sqlalchemy import text
    db2.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    db2.session.commit()
    db2.session.close()
    db2.engine.dispose()

    print(f"\nDeleted {deleted} orphan rows.")


if __name__ == "__main__":
    main()
