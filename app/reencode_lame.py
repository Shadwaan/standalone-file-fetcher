"""
Re-encode unanalyzed MP3s in the keep-playlists through the REAL lame.exe
binary, so they get a proper LAME3.100 header (with encoder_delay and
encoder_padding) that Rekordbox's parser fully trusts.

Pipeline per file:
  1. ffmpeg   decodes the MP3 to temp WAV (preserves sample rate)
  2. lame.exe encodes WAV to new MP3 with real LAME header
  3. mutagen  copies all ID3 tags from original to new MP3
  4. verify   new MP3's Xing header contains 'LAME' identifier
  5. backup   original to <file>.bak
  6. swap     new MP3 into the original path

No DB writes. File paths stay the same.

Usage:
    python reencode_lame.py             # dry run
    python reencode_lame.py --apply     # execute
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

FFMPEG = (
    r"C:\Users\Lenovo\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
)
LAME = r"C:\Users\Lenovo\AppData\Roaming\lexicon\lib\lame\lame.exe"

KEEP_PLAYLISTS = ["Half moon", "Progressive", "Opening", "Oldies"]


def verify_lame_header(fp: Path) -> bool:
    """Confirm Xing header has real 'LAME' identifier (not 'Lavc')."""
    try:
        with open(fp, "rb") as f:
            head = f.read(10)
            id3 = 0
            if head[:3] == b"ID3":
                id3 = ((head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]) + 10
            f.seek(id3)
            blob = f.read(4096)
    except Exception:
        return False
    # Find frame sync, skip 32-byte side info, find Xing/Info then LAME
    for i in range(len(blob) - 4):
        if blob[i] == 0xFF and (blob[i + 1] & 0xE0) == 0xE0:
            xing_off = i + 4 + 32
            if xing_off + 4 > len(blob):
                return False
            tag = blob[xing_off:xing_off + 4]
            if tag not in (b"Xing", b"Info"):
                return False
            # LAME tag search in next 200 bytes
            region = blob[xing_off:xing_off + 200]
            return b"LAME" in region and b"Lavc" not in region[:150]
    return False


def reencode_one(src: Path, dst: Path) -> tuple[bool, str]:
    """Decode with ffmpeg, encode with LAME, copy tags."""
    tmp_wav = dst.with_suffix(".reenc.wav")

    # 1. Decode to 16-bit PCM WAV (preserve source SR)
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error",
         "-i", str(src), "-vn", "-c:a", "pcm_s16le", "-y", str(tmp_wav)],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0 or not tmp_wav.exists():
        return False, f"ffmpeg decode failed: {r.stderr[:200]}"

    try:
        # 2. Encode with LAME
        r = subprocess.run(
            [LAME, "--quiet", "-b", "320", "--cbr", "-h",
             str(tmp_wav), str(dst)],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0 or not dst.exists():
            return False, f"LAME encode failed: {r.stderr[:200]}"

        # 3. Copy ID3 tags from original
        try:
            from mutagen.id3 import ID3
            src_tags = ID3(str(src))
            src_tags.save(str(dst), v2_version=3)
        except Exception as e:
            return False, f"tag copy failed: {e}"

        # 4. Verify LAME header in output
        if not verify_lame_header(dst):
            return False, "no LAME identifier in output Xing header"

        return True, "ok"
    finally:
        if tmp_wav.exists():
            tmp_wav.unlink()


def main():
    apply = "--apply" in sys.argv

    for p, label in [(FFMPEG, "ffmpeg"), (LAME, "lame")]:
        if not os.path.exists(p):
            print(f"ERROR: {label} not found at {p}")
            sys.exit(1)

    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables
    from mutagen.mp3 import MP3

    db = Rekordbox6Database()

    # Gather unanalyzed MP3s from the keep playlists
    targets: list[tuple[str, Path, int, str]] = []  # (title, path, analysed, cid)
    seen: set[str] = set()

    for pl_name in KEEP_PLAYLISTS:
        pl = db.session.query(tables.DjmdPlaylist).filter_by(Name=pl_name).first()
        if not pl:
            print(f"WARN: playlist '{pl_name}' not found, skipping")
            continue
        songs = db.session.query(tables.DjmdSongPlaylist).filter_by(
            PlaylistID=pl.ID
        ).all()
        for s in songs:
            if str(s.ContentID) in seen:
                continue
            seen.add(str(s.ContentID))
            c = db.session.query(tables.DjmdContent).filter_by(
                ID=s.ContentID
            ).first()
            if not c:
                continue
            if c.Analysed == 105:
                continue
            fp = Path(str(c.FolderPath or ""))
            if not fp.exists() or fp.suffix.lower() != ".mp3":
                continue
            targets.append((c.Title or fp.stem, fp, c.Analysed, str(c.ID)))

    db.session.close()
    db.engine.dispose()

    print(f"Target unanalyzed MP3s across keep playlists ({len(targets)}):")
    for title, fp, analysed, _ in targets:
        size_mb = fp.stat().st_size / (1024 * 1024)
        try:
            sr = MP3(str(fp)).info.sample_rate
        except Exception:
            sr = "?"
        print(f"  {title[:50]:<50}  {size_mb:5.1f} MB  SR={sr}  Analysed={analysed}")

    if not targets:
        print("Nothing to re-encode.")
        return

    if not apply:
        print("\n(dry run — re-run with --apply to execute)")
        return

    # Safety: if any targets have stuck Analysed flags, we'll reset them to 0
    # post-encode. That requires Rekordbox to be closed.
    stuck = [t for t in targets if t[2] not in (0, None)]
    if stuck:
        import psutil
        for p in psutil.process_iter(["name"]):
            try:
                if "rekordbox" in (p.info["name"] or "").lower():
                    print(f"ERROR: Rekordbox is running. Close it (and agents) before --apply.")
                    sys.exit(1)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    print()
    ok, fail = 0, 0
    reset_cids: list[str] = []
    for title, fp, analysed, cid in targets:
        print(f"Re-encoding: {title[:50]}")
        bak = fp.with_suffix(fp.suffix + ".bak")
        tmp_out = fp.with_suffix(".reenc.mp3")

        try:
            shutil.copy2(fp, bak)
        except Exception as e:
            print(f"  FAIL backup: {e}")
            fail += 1
            continue

        success, msg = reencode_one(fp, tmp_out)
        if not success:
            print(f"  FAIL: {msg}")
            if tmp_out.exists():
                tmp_out.unlink()
            fail += 1
            continue

        try:
            os.replace(tmp_out, fp)
        except Exception as e:
            print(f"  FAIL swap: {e}")
            fail += 1
            continue

        new_mb = fp.stat().st_size / (1024 * 1024)
        print(f"  OK: {new_mb:.1f} MB, LAME header verified, backup at {bak.name}")
        ok += 1
        if analysed not in (0, None):
            reset_cids.append(cid)

    # Reset stuck Analysed flags so Rekordbox retries cleanly
    if reset_cids:
        print(f"\nResetting Analysed flag to 0 on {len(reset_cids)} stuck rows...")
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

    print(f"\nDone: {ok} re-encoded, {fail} failed.")
    print(
        "Delete <file>.bak backups manually after confirming Rekordbox "
        "analyses the new files cleanly."
    )


if __name__ == "__main__":
    main()
