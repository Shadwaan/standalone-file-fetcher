"""
Nuke Moroccan Moonlight from Rekordbox:

  1. For every track currently in any 'Moroccan Moonlight' playlist:
       - If the track is ALSO in any other playlist (any non-MM playlist), keep
         the track row in the library, just unlink from MM playlists.
       - Otherwise, delete the DjmdContent row entirely and move the MP3 file
         to a quarantine folder outside the normal music tree.
  2. Delete the Moroccan Moonlight playlist rows themselves.
  3. master.db is backed up before --apply. Rekordbox must be closed.

Does NOT touch sff's sync_state.json or Spotify. If you want to prevent
re-download on next sff sync, rename / remove the Spotify playlist's 'FF'
prefix on the Spotify side.

Usage:
    python purge_moroccan_moonlight.py             # dry run
    python purge_moroccan_moonlight.py --apply     # execute
"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


PURGE_NAME = "Moroccan Moonlight"
QUARANTINE = Path("D:/Music Backup/_quarantine_moroccan_moonlight")


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
        print("ERROR: Rekordbox is running. Close it before applying.")
        sys.exit(1)

    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    db = Rekordbox6Database()

    mm_playlists = db.session.query(tables.DjmdPlaylist).filter_by(
        Name=PURGE_NAME
    ).all()

    if not mm_playlists:
        print(f"No playlist named '{PURGE_NAME}' found.")
        return

    mm_pids = {str(pl.ID) for pl in mm_playlists}
    print(f"Target playlists ({len(mm_playlists)}):")
    for pl in mm_playlists:
        n = db.session.query(tables.DjmdSongPlaylist).filter_by(
            PlaylistID=pl.ID
        ).count()
        print(f"  ID={pl.ID}  tracks={n}")

    # Gather every content_id in any MM playlist
    mm_song_rows = db.session.query(tables.DjmdSongPlaylist).filter(
        tables.DjmdSongPlaylist.PlaylistID.in_(mm_pids)
    ).all()
    mm_content_ids = {str(s.ContentID) for s in mm_song_rows}
    print(f"\nUnique tracks across MM playlists: {len(mm_content_ids)}")

    # Classify: kept (also in other playlists) vs orphaned (only in MM)
    kept: list[dict] = []
    orphan: list[dict] = []
    missing_file: list[dict] = []

    for cid in mm_content_ids:
        content = db.session.query(tables.DjmdContent).filter_by(ID=cid).first()
        if not content:
            continue
        title = content.Title or "(no title)"
        fp = str(content.FolderPath or "")

        other_links = (
            db.session.query(tables.DjmdSongPlaylist)
            .filter(
                tables.DjmdSongPlaylist.ContentID == cid,
                ~tables.DjmdSongPlaylist.PlaylistID.in_(mm_pids),
            )
            .count()
        )

        entry = {"cid": cid, "title": title, "file": fp, "other_links": other_links}

        if other_links > 0:
            kept.append(entry)
        else:
            if fp and not Path(fp).exists():
                missing_file.append(entry)
            orphan.append(entry)

    print(f"\n--- KEPT IN LIBRARY ({len(kept)}) — only unlinked from MM ---")
    for e in kept[:10]:
        print(f"  {e['cid']}  '{e['title'][:50]}'  (in {e['other_links']} other playlist(s))")
    if len(kept) > 10:
        print(f"  ... and {len(kept) - 10} more")

    print(f"\n--- ORPHANED — will be removed from library + files quarantined ({len(orphan)}) ---")
    for e in orphan[:20]:
        exists = Path(e["file"]).exists() if e["file"] else False
        marker = "" if exists else "  (file already missing)"
        print(f"  {e['cid']}  '{e['title'][:50]}'{marker}")
    if len(orphan) > 20:
        print(f"  ... and {len(orphan) - 20} more")

    print(f"\n--- Summary ---")
    print(f"  Keep + unlink: {len(kept)}")
    print(f"  Orphan delete: {len(orphan)}  (of which {len(missing_file)} have no file on disk)")
    print(f"  Playlists to delete: {len(mm_playlists)}")
    print(f"  Quarantine folder: {QUARANTINE}")

    if not apply:
        print("\n(dry run — re-run with --apply to execute)")
        db.session.close()
        db.engine.dispose()
        return

    db.session.close()
    db.engine.dispose()

    backup_db()
    QUARANTINE.mkdir(parents=True, exist_ok=True)

    db2 = Rekordbox6Database()

    # Step 1: move MP3 files for orphans + delete DjmdContent rows
    quarantined = 0
    content_deleted = 0
    for e in orphan:
        fp = Path(e["file"]) if e["file"] else None
        if fp and fp.exists():
            dst = QUARANTINE / fp.name
            n = 1
            while dst.exists():
                dst = QUARANTINE / f"{fp.stem}_{n}{fp.suffix}"
                n += 1
            try:
                shutil.move(str(fp), str(dst))
                quarantined += 1
            except Exception as ex:
                print(f"  WARN: could not move {fp.name}: {ex}")

        # Delete any remaining song-playlist links (across all playlists)
        links = db2.session.query(tables.DjmdSongPlaylist).filter_by(
            ContentID=e["cid"]
        ).all()
        for l in links:
            db2.session.delete(l)

        c = db2.session.query(tables.DjmdContent).filter_by(ID=e["cid"]).first()
        if c:
            db2.session.delete(c)
            content_deleted += 1

    # Step 2: unlink kept tracks from MM playlists
    unlinks = 0
    for e in kept:
        links = db2.session.query(tables.DjmdSongPlaylist).filter(
            tables.DjmdSongPlaylist.ContentID == e["cid"],
            tables.DjmdSongPlaylist.PlaylistID.in_(mm_pids),
        ).all()
        for l in links:
            db2.session.delete(l)
            unlinks += 1

    # Step 3: delete MM playlist rows
    playlists_deleted = 0
    for pid in mm_pids:
        pl = db2.session.query(tables.DjmdPlaylist).filter_by(ID=pid).first()
        if pl:
            # Final sweep of any song rows under this playlist
            stragglers = db2.session.query(tables.DjmdSongPlaylist).filter_by(
                PlaylistID=pid
            ).all()
            for s in stragglers:
                db2.session.delete(s)
            db2.session.delete(pl)
            playlists_deleted += 1

    db2.session.commit()

    from sqlalchemy import text
    db2.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    db2.session.commit()
    db2.session.close()
    db2.engine.dispose()

    print(f"\n=== Applied ===")
    print(f"  Files quarantined: {quarantined} -> {QUARANTINE}")
    print(f"  DjmdContent rows deleted: {content_deleted}")
    print(f"  Kept tracks unlinked from MM: {unlinks}")
    print(f"  MM playlists deleted: {playlists_deleted}")


if __name__ == "__main__":
    main()
