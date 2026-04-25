"""
Three-pass master.db maintenance:

  1. PRAGMA integrity_check  — reports any SQLite-level corruption (read-only)
  2. Reset Analysed != (0 or 105) rows back to 0 — clears stuck half-analysis
     states that have accumulated from crashes over time
  3. VACUUM  — rebuilds DB pages, compacts fragmentation, reduces lock-contention
     surface

Rekordbox must be closed. master.db is backed up before any writes.

Usage:
    python db_maintenance.py             # dry run (read + report only)
    python db_maintenance.py --apply     # do the writes
"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


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
        print("ERROR: Rekordbox is running. Close it (and agents) before --apply.")
        sys.exit(1)

    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables
    from sqlalchemy import text

    db = Rekordbox6Database()

    # --- Pass 1: integrity check ---
    print("=== 1. PRAGMA integrity_check ===")
    result = db.session.execute(text("PRAGMA integrity_check")).fetchall()
    for row in result:
        print(f"  {row[0]}")
    is_clean = len(result) == 1 and result[0][0] == "ok"

    # --- Pass 2: TRULY stuck Analysed states ---
    # Safe filter: Analysed not in (0, 105) AND no BPM AND no ANLZ path.
    # Other non-standard Analysed values (17, 88, 121) represent legitimate
    # analyses with different option sets — touching them would destroy data.
    print("\n=== 2. Truly stuck rows (no BPM, no ANLZ, non-standard Analysed) ===")
    stuck_rows = db.session.query(tables.DjmdContent).filter(
        tables.DjmdContent.Analysed != 105,
        tables.DjmdContent.Analysed != 0,
        tables.DjmdContent.BPM.is_(None),
        tables.DjmdContent.AnalysisDataPath.is_(None),
    ).all()
    print(f"  Will reset to Analysed=0: {len(stuck_rows)}")
    for r in stuck_rows:
        print(f"    CID={r.ID}  Analysed={r.Analysed}  '{(r.Title or '')[:60]}'")

    # --- Pass 3: DB file size ---
    print("\n=== 3. DB file size (pre-VACUUM) ===")
    db_dir = Path(os.environ["APPDATA"]) / "Pioneer" / "rekordbox"
    db_size = (db_dir / "master.db").stat().st_size
    wal_path = db_dir / "master.db-wal"
    wal_size = wal_path.stat().st_size if wal_path.exists() else 0
    print(f"  master.db:     {db_size:>12,} bytes ({db_size / 1024 / 1024:.1f} MB)")
    print(f"  master.db-wal: {wal_size:>12,} bytes")

    if not apply:
        print("\n(dry run — re-run with --apply to execute pass 2 + pass 3)")
        db.session.close()
        db.engine.dispose()
        return

    if not is_clean:
        print("\nINTEGRITY CHECK FAILED — not proceeding with writes.")
        print("Manual repair needed. Exiting.")
        db.session.close()
        db.engine.dispose()
        sys.exit(1)

    db.session.close()
    db.engine.dispose()

    # --- Apply writes ---
    backup_db()

    print("\nResetting truly-stuck Analysed flags to 0...")
    db2 = Rekordbox6Database()
    n_reset = db2.session.query(tables.DjmdContent).filter(
        tables.DjmdContent.Analysed != 105,
        tables.DjmdContent.Analysed != 0,
        tables.DjmdContent.BPM.is_(None),
        tables.DjmdContent.AnalysisDataPath.is_(None),
    ).update({tables.DjmdContent.Analysed: 0}, synchronize_session=False)
    db2.session.commit()
    print(f"  Reset {n_reset} rows.")

    print("\nCheckpointing WAL...")
    db2.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    db2.session.commit()
    db2.session.close()
    db2.engine.dispose()

    # VACUUM via pyrekordbox (SQLCipher-aware).
    print("\nRunning VACUUM via SQLCipher-aware connection (may take a minute)...")
    db3 = Rekordbox6Database()
    db3.session.commit()
    db3.session.close()
    with db3.engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.exec_driver_sql("VACUUM")
    db3.engine.dispose()

    new_size = (db_dir / "master.db").stat().st_size
    print(f"  master.db now: {new_size:>12,} bytes ({new_size / 1024 / 1024:.1f} MB)")
    print(f"  Delta:         {db_size - new_size:>+12,} bytes")

    print("\nDone. Open Rekordbox and retry analysis.")


if __name__ == "__main__":
    main()
