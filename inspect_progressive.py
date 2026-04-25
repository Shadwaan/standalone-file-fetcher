"""
READ-ONLY inspection of the 'Progressive' / 'FF Progressive' playlist.

For each track, distinguishes:
  - REKORDBOX-NATIVE analysis  (ANLZ path does NOT match uuid5(NAMESPACE_DNS, content.ID))
  - SFF-PLANTED analysis       (ANLZ path DOES match the deterministic sff uuid)
  - Clean / unanalyzed         (Analysed=0, no ANLZ path)
  - Weird                      (non-0/105 Analysed, or inconsistent state)

Does NOT modify anything.
"""

import os
import uuid
from pathlib import Path


def sff_expected_uuid(content_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, str(content_id)))


def classify(content_id, anlz_path: str) -> str:
    if not anlz_path:
        return "no-anlz"
    expected = sff_expected_uuid(content_id)
    if expected in anlz_path:
        return "SFF-PLANTED"
    # Rekordbox-native: usually short hash dir + ANLZxxxx.DAT with non-zero suffix
    return "RB-NATIVE"


def main():
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    db = Rekordbox6Database()

    playlist = None
    for name in ("Progressive", "FF Progressive", "FF progressive", "progressive"):
        pl = db.session.query(tables.DjmdPlaylist).filter_by(Name=name).first()
        if pl:
            playlist = pl
            break
    if not playlist:
        for pl in db.session.query(tables.DjmdPlaylist).all():
            if pl.Name and "progressive" in pl.Name.lower():
                playlist = pl
                break

    if not playlist:
        print("No Progressive playlist found.")
        return

    print(f"Playlist: '{playlist.Name}' (ID={playlist.ID})")
    print("=" * 120)

    share_root = os.path.join(
        os.environ.get("APPDATA", ""), "Pioneer", "rekordbox", "share"
    )

    songs = (
        db.session.query(tables.DjmdSongPlaylist)
        .filter_by(PlaylistID=playlist.ID)
        .order_by(tables.DjmdSongPlaylist.TrackNo)
        .all()
    )

    buckets = {"SFF-PLANTED": [], "RB-NATIVE": [], "CLEAN": [], "WEIRD": []}

    for song in songs:
        content = db.session.query(tables.DjmdContent).filter_by(
            ID=song.ContentID
        ).first()
        if not content:
            continue

        fp = str(getattr(content, "FolderPath", "") or "")
        title = getattr(content, "Title", "") or "(no title)"
        analysed = getattr(content, "Analysed", None)
        bpm_raw = getattr(content, "BPM", None)
        bpm = (bpm_raw / 100.0) if bpm_raw else None
        anlz_path = getattr(content, "AnalysisDataPath", "") or ""

        klass = classify(content.ID, anlz_path)
        expected_sff_uuid = sff_expected_uuid(content.ID)

        dat_exists = False
        if anlz_path:
            dat_abs = os.path.join(share_root, anlz_path.lstrip("/"))
            dat_exists = os.path.exists(dat_abs)

        row = {
            "track_no": song.TrackNo,
            "title": title,
            "content_id": content.ID,
            "analysed": analysed,
            "bpm": bpm,
            "anlz_path": anlz_path,
            "dat_exists": dat_exists,
            "expected_sff_uuid": expected_sff_uuid,
            "file": fp,
        }

        if klass == "SFF-PLANTED":
            buckets["SFF-PLANTED"].append(row)
        elif klass == "RB-NATIVE":
            buckets["RB-NATIVE"].append(row)
        elif klass == "no-anlz":
            if analysed in (0, None) and not bpm:
                buckets["CLEAN"].append(row)
            else:
                buckets["WEIRD"].append(row)
        else:
            buckets["WEIRD"].append(row)

    def fmt(row):
        bpm_s = f"{row['bpm']:.2f}" if row["bpm"] else "-"
        return (
            f"  #{row['track_no']:>3}  "
            f"{row['title'][:50]:<50}  "
            f"CID={row['content_id']:<11}  "
            f"Analysed={str(row['analysed']):<4}  "
            f"BPM={bpm_s:<6}  "
            f"DAT={'Y' if row['dat_exists'] else 'N'}  "
            f"ANLZ={row['anlz_path']}"
        )

    for label in ("SFF-PLANTED", "RB-NATIVE", "WEIRD", "CLEAN"):
        rows = buckets[label]
        print(f"\n--- {label}  ({len(rows)}) ---")
        for row in rows:
            print(fmt(row))
            if label == "SFF-PLANTED":
                print(f"        expected_sff_uuid = {row['expected_sff_uuid']}")

    print("\n" + "=" * 120)
    print(
        f"Summary: "
        f"{len(buckets['SFF-PLANTED'])} sff-planted, "
        f"{len(buckets['RB-NATIVE'])} rb-native, "
        f"{len(buckets['WEIRD'])} weird, "
        f"{len(buckets['CLEAN'])} clean"
    )

    db.session.close()
    db.engine.dispose()


if __name__ == "__main__":
    main()
