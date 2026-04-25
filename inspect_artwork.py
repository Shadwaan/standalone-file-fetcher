"""
READ-ONLY: extract embedded APIC artwork from every Progressive MP3
and hash it. Identical hashes = same artwork across multiple files.
Also dumps the DjmdContent.AlbumID and related fields from master.db.
"""

import hashlib
from pathlib import Path

from mutagen.id3 import ID3


def artwork_info(fp: Path) -> dict:
    try:
        tags = ID3(str(fp))
    except Exception as e:
        return {"error": str(e)}
    info = {"apic_count": 0, "entries": []}
    for key in tags.keys():
        if not key.startswith("APIC"):
            continue
        frame = tags[key]
        data = getattr(frame, "data", b"")
        h = hashlib.sha1(data).hexdigest()[:12]
        info["apic_count"] += 1
        info["entries"].append({
            "desc": getattr(frame, "desc", "") or "(no desc)",
            "mime": getattr(frame, "mime", "?"),
            "size": len(data),
            "sha1_12": h,
        })
    return info


def main():
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    db = Rekordbox6Database()

    playlist = db.session.query(tables.DjmdPlaylist).filter_by(
        Name="Progressive"
    ).first()
    songs = (
        db.session.query(tables.DjmdSongPlaylist)
        .filter_by(PlaylistID=playlist.ID)
        .order_by(tables.DjmdSongPlaylist.TrackNo)
        .all()
    )

    # Group by artwork hash to see duplicates
    hash_to_titles: dict[str, list[str]] = {}

    print(f"{'Track':<50}  {'Size':>8}  {'SHA1[:12]':<14}  AlbumID")
    print("-" * 110)

    for song in songs:
        content = db.session.query(tables.DjmdContent).filter_by(
            ID=song.ContentID
        ).first()
        if not content:
            continue
        title = (content.Title or "")[:48]
        fp = Path(str(content.FolderPath))
        album_id = content.AlbumID
        image_path = getattr(content, "ImagePath", None)

        if fp.exists():
            info = artwork_info(fp)
            if info.get("entries"):
                for e in info["entries"]:
                    h = e["sha1_12"]
                    hash_to_titles.setdefault(h, []).append(title)
                    print(
                        f"{title:<50}  {e['size']:>8}  {h:<14}  "
                        f"{album_id}  img={image_path}"
                    )
            else:
                print(f"{title:<50}  {'-':>8}  {'(no APIC)':<14}  {album_id}")
        else:
            print(f"{title:<50}  (file missing)")

    print("\n--- Duplicates ---")
    any_dupes = False
    for h, titles in hash_to_titles.items():
        if len(titles) > 1:
            any_dupes = True
            print(f"  {h}  used by {len(titles)} tracks:")
            for t in titles:
                print(f"    - {t}")
    if not any_dupes:
        print("  (none — every track has unique artwork)")

    db.session.close()
    db.engine.dispose()


if __name__ == "__main__":
    main()
