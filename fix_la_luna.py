"""
Remove the WRONG La Luna (CID 264375216, the '[SPOTDOWNLOADER.COM]' file
in Braindead) from the 'Progressive' playlist only.

Both DjmdContent rows remain in the library. Only the DjmdSongPlaylist row
linking the wrong one to Progressive is deleted.

Usage:
    python fix_la_luna.py             # dry run, prints what would change
    python fix_la_luna.py --apply     # apply after making a master.db backup
"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


WRONG_CID = "264375216"   # [SPOTDOWNLOADER.COM] La Luna ... (Braindead)
RIGHT_CID = "182258202"   # Nic Fanciulli (Melodic)
PLAYLIST_NAME = "Progressive"


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

    # Check both CIDs exist and are in the playlist
    wrong_link = db.session.query(tables.DjmdSongPlaylist).filter_by(
        PlaylistID=playlist.ID, ContentID=WRONG_CID
    ).first()
    right_link = db.session.query(tables.DjmdSongPlaylist).filter_by(
        PlaylistID=playlist.ID, ContentID=RIGHT_CID
    ).first()

    def describe(cid, link):
        c = db.session.query(tables.DjmdContent).filter_by(ID=cid).first()
        if not c:
            return f"CID={cid} NOT FOUND"
        fp = c.FolderPath
        link_s = f"TrackNo={link.TrackNo}" if link else "NOT IN PLAYLIST"
        return f"CID={cid}  {link_s}  {fp}"

    print("Before:")
    print(f"  WRONG: {describe(WRONG_CID, wrong_link)}")
    print(f"  RIGHT: {describe(RIGHT_CID, right_link)}")

    if not wrong_link:
        print("\nWrong CID is already not in the playlist. Nothing to do.")
        db.session.close()
        db.engine.dispose()
        return

    print(f"\nPlanned change: DELETE DjmdSongPlaylist row "
          f"(PlaylistID={playlist.ID}, ContentID={WRONG_CID}, TrackNo={wrong_link.TrackNo})")
    print("Library entries for both tracks will remain untouched.")

    if not apply:
        print("\n(dry run — re-run with --apply to execute)")
        db.session.close()
        db.engine.dispose()
        return

    # Close read session, back up, then open fresh session for writes
    db.session.close()
    db.engine.dispose()

    backup_db()

    # Open a fresh session and re-fetch the link
    db2 = Rekordbox6Database()
    link = db2.session.query(tables.DjmdSongPlaylist).filter_by(
        PlaylistID=playlist.ID, ContentID=WRONG_CID
    ).first()
    db2.session.delete(link)
    db2.session.commit()

    # Flush WAL so Rekordbox sees the change immediately
    from sqlalchemy import text
    db2.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    db2.session.commit()
    db2.session.close()
    db2.engine.dispose()

    print("\nApplied. Re-open Rekordbox to verify the playlist.")


if __name__ == "__main__":
    main()
