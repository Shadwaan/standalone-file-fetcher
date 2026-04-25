"""
READ-ONLY: dump ID3 tags of every MP3 in the Progressive playlist.

Looks specifically for analysis-related frames Rekordbox may trust:
  TBPM         — BPM
  TKEY         — initial key
  COMM         — comments (Serato/MIK sometimes hide markers here)
  GEOB         — generic encapsulated objects (Serato/Traktor beatgrids)
  PRIV         — private frames (DJ-software beatgrids)
  XXX:BeatGrid / Mixed In Key / Serato / Traktor4 — any known DJ markers
"""

from pathlib import Path
from mutagen.id3 import ID3


ANALYSIS_FRAMES = ("TBPM", "TKEY", "GEOB", "PRIV", "TXXX")
SUSPICIOUS_SUBSTRINGS = (
    "serato", "traktor", "mixed in key", "mik", "beatgrid",
    "beat grid", "cue", "analysis", "rekordbox",
)


def dump(fp: Path):
    print(f"\n=== {fp.name}")
    try:
        tags = ID3(str(fp))
    except Exception as e:
        print(f"  (no ID3 or error: {e})")
        return

    all_keys = list(tags.keys())
    print(f"  all frames: {sorted(set(k.split(':')[0] for k in all_keys))}")

    for key in all_keys:
        frame_id = key.split(":")[0]
        safe_key = key.encode("ascii", "backslashreplace").decode("ascii")
        if frame_id not in ANALYSIS_FRAMES:
            continue
        frame = tags[key]
        try:
            val = str(frame)[:200]
        except Exception:
            val = f"<unreadable {type(frame).__name__}>"
        # Sanitize non-ASCII for console
        val = val.encode("ascii", "backslashreplace").decode("ascii")
        vlow = val.lower()
        flag = ""
        if any(s in vlow for s in SUSPICIOUS_SUBSTRINGS):
            flag = "  <-- DJ marker"
        # PRIV frames need special handling: show owner + data length
        if frame_id == "PRIV":
            owner = str(getattr(frame, "owner", "?")).encode("ascii", "backslashreplace").decode("ascii")
            data_len = len(getattr(frame, "data", b""))
            print(f"  PRIV: owner={owner}  data_len={data_len}  <-- DJ SOFTWARE MARKER")
        else:
            print(f"  {safe_key}: {val}{flag}")


def main():
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    db = Rekordbox6Database()

    playlist = None
    for name in ("Progressive", "FF Progressive"):
        pl = db.session.query(tables.DjmdPlaylist).filter_by(Name=name).first()
        if pl:
            playlist = pl
            break

    songs = (
        db.session.query(tables.DjmdSongPlaylist)
        .filter_by(PlaylistID=playlist.ID)
        .order_by(tables.DjmdSongPlaylist.TrackNo)
        .all()
    )

    for song in songs:
        content = db.session.query(tables.DjmdContent).filter_by(
            ID=song.ContentID
        ).first()
        if not content:
            continue
        fp = Path(str(getattr(content, "FolderPath", "")))
        if not fp.exists() or fp.suffix.lower() != ".mp3":
            print(f"\n=== (skip) {fp}  — not a local mp3")
            continue
        dump(fp)

    db.session.close()
    db.engine.dispose()


if __name__ == "__main__":
    main()
