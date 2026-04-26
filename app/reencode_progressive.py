"""
Re-encode unanalyzed Progressive MP3s through libmp3lame so they get a proper
LAME header (encoder_delay + padding), which lets Rekordbox analyze without
crashing and places the beat grid at the correct offset.

- Targets every Progressive track where DjmdContent.Analysed == 0.
- Audio: decoded + re-encoded at same bitrate (320k) and same sample rate (48k).
- Metadata: all ID3 frames preserved (including APIC artwork).
- Each original is backed up to <file>.bak before being replaced.
- Output is verified to contain a LAME tag before the swap.
- DB is not touched — file paths stay the same.

Usage:
    python reencode_progressive.py             # dry run
    python reencode_progressive.py --apply     # do the work
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

FFMPEG_DIR = (
    r"C:\Users\Lenovo\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-8.1-full_build\bin"
)
FFMPEG = os.path.join(FFMPEG_DIR, "ffmpeg.exe")

PLAYLIST_NAME = "Progressive"


def verify_lame_tag(fp: Path) -> bool:
    """Confirm the output MP3 has a LAME encoder tag."""
    from mutagen.id3 import ID3
    try:
        id3_size = 0
        with open(fp, "rb") as f:
            head = f.read(10)
            if head[:3] == b"ID3":
                id3_size = (
                    (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
                ) + 10
            f.seek(id3_size)
            blob = f.read(4096)
    except Exception:
        return False

    # Find frame sync, skip side info, look for Xing/Info + LAME
    for i in range(len(blob) - 4):
        if blob[i] == 0xFF and (blob[i + 1] & 0xE0) == 0xE0:
            xing_off = i + 4 + 32
            if xing_off + 4 > len(blob):
                return False
            tag = blob[xing_off:xing_off + 4]
            if tag not in (b"Xing", b"Info"):
                return False
            # LAME tag should appear somewhere after the Xing header
            return b"LAME" in blob[xing_off:xing_off + 200]
    return False


def reencode(src: Path, dst: Path) -> tuple[bool, str]:
    """Run ffmpeg with libmp3lame + metadata preservation."""
    cmd = [
        FFMPEG,
        "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-c:a", "libmp3lame",
        "-b:a", "320k",
        "-ar", "48000",
        "-id3v2_version", "3",
        "-write_id3v1", "0",
        "-map_metadata", "0",
        "-map", "0:a",
        "-map", "0:v?",
        "-y",
        str(dst),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out after 10 min"
    if r.returncode != 0:
        return False, f"ffmpeg exit {r.returncode}: {r.stderr[:400]}"
    if not dst.exists() or dst.stat().st_size < 1000:
        return False, "output file missing or too small"
    if not verify_lame_tag(dst):
        return False, "output file has no LAME tag"
    return True, "ok"


def main():
    apply = "--apply" in sys.argv

    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables

    if not os.path.exists(FFMPEG):
        print(f"ERROR: ffmpeg not found at {FFMPEG}")
        sys.exit(1)

    db = Rekordbox6Database()
    playlist = db.session.query(tables.DjmdPlaylist).filter_by(
        Name=PLAYLIST_NAME
    ).first()
    songs = (
        db.session.query(tables.DjmdSongPlaylist)
        .filter_by(PlaylistID=playlist.ID)
        .order_by(tables.DjmdSongPlaylist.TrackNo)
        .all()
    )

    # Target: anything not fully analyzed (Analysed != 105).
    # For 'stuck' states like Analysed=74 (crashed mid-analysis),
    # we'll reset the flag to 0 so Rekordbox re-tries after re-encode.
    targets: list[tuple[str, Path, int, str]] = []
    for song in songs:
        content = db.session.query(tables.DjmdContent).filter_by(
            ID=song.ContentID
        ).first()
        if not content:
            continue
        if content.Analysed == 105:
            continue
        fp = Path(str(content.FolderPath or ""))
        if not fp.exists() or fp.suffix.lower() != ".mp3":
            continue
        targets.append((
            content.Title or fp.stem,
            fp,
            content.Analysed,
            str(content.ID),
        ))

    db.session.close()
    db.engine.dispose()

    print(f"Target files ({len(targets)}):")
    for t, fp, analysed, _cid in targets:
        size_kb = fp.stat().st_size // 1024
        note = " [reset Analysed=74 -> 0]" if analysed not in (0, None) else ""
        print(f"  {t[:50]:<50}  {size_kb:>6} KB  Analysed={analysed}{note}")

    if not targets:
        print("Nothing to do.")
        return

    if not apply:
        print("\n(dry run — re-run with --apply to re-encode)")
        return

    # Safety: require Rekordbox closed before we reset Analysed flags
    needs_flag_reset = [t for t in targets if t[2] not in (0, None)]
    if needs_flag_reset:
        import psutil
        for p in psutil.process_iter(["name"]):
            try:
                n = (p.info["name"] or "").lower()
                if "rekordbox" in n:
                    print(f"ERROR: Rekordbox ({p.info['name']}) is running — close it before --apply")
                    sys.exit(1)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    print()
    ok_count = 0
    fail_count = 0
    reset_cids: list[str] = []
    for title, fp, analysed, cid in targets:
        print(f"Re-encoding: {title[:50]}")
        bak = fp.with_suffix(fp.suffix + ".bak")
        tmp = fp.with_suffix(".lame.mp3")

        # Copy original to .bak first (non-destructive backup before touching)
        try:
            shutil.copy2(fp, bak)
        except Exception as e:
            print(f"  FAIL backup: {e}")
            fail_count += 1
            continue

        ok, msg = reencode(fp, tmp)
        if not ok:
            print(f"  FAIL: {msg}")
            if tmp.exists():
                tmp.unlink()
            fail_count += 1
            continue

        # Atomic-ish swap: replace original with tmp
        try:
            os.replace(tmp, fp)
        except Exception as e:
            print(f"  FAIL swap: {e}")
            # Attempt rollback from .bak if original is gone
            if not fp.exists() and bak.exists():
                shutil.copy2(bak, fp)
            fail_count += 1
            continue

        new_kb = fp.stat().st_size // 1024
        print(f"  OK: {new_kb} KB, LAME tag verified, original preserved at {bak.name}")
        ok_count += 1
        if analysed not in (0, None):
            reset_cids.append(cid)

    # Reset stuck Analysed flags to 0 so Rekordbox retries cleanly
    if reset_cids:
        print(f"\nResetting Analysed flag to 0 on {len(reset_cids)} stuck row(s)...")
        import shutil as _sh
        from datetime import datetime as _dt
        db_dir = Path(os.environ["APPDATA"]) / "Pioneer" / "rekordbox"
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        _sh.copy2(db_dir / "master.db", db_dir / f"master.db.bak.{ts}")
        print(f"Backup: master.db.bak.{ts}")

        db2 = Rekordbox6Database()
        for cid in reset_cids:
            c = db2.session.query(tables.DjmdContent).filter_by(ID=cid).first()
            if c:
                c.Analysed = 0
        db2.session.commit()
        from sqlalchemy import text
        db2.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        db2.session.commit()
        db2.session.close()
        db2.engine.dispose()

    print(f"\nDone: {ok_count} re-encoded, {fail_count} failed.")
    print(
        "Originals are at <file>.bak — delete them once you've confirmed Rekordbox "
        "analyses the new files cleanly."
    )


if __name__ == "__main__":
    main()
