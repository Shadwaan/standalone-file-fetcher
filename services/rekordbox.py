"""
Rekordbox import service.

Creates DjmdContent entries in master.db (via pyrekordbox),
writes CDJ-compatible ANLZ files (.DAT + .EXT),
and manages playlists (DjmdPlaylist + DjmdSongPlaylist).
"""

import logging
import os
import struct
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from models.track import AnalysisResult, TrackInfo

logger = logging.getLogger(__name__)

ANLZ_ROOT = os.getenv(
    "ANLZ_ROOT",
    os.path.join(os.environ.get("APPDATA", ""), "Pioneer", "rekordbox", "share", "PIONEER", "USBANLZ"),
)

# File header version bytes (matches real Rekordbox output)
FILE_HEADER_FLAGS = b"\x00\x00\x00\x01\x00\x01\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"


# ─── ANLZ Binary Writers ────────────────────────────────────────────────────

def _build_file_header(total_len: int) -> bytes:
    """Build PMAI file header (28 bytes)."""
    return struct.pack(">4sII", b"PMAI", 28, total_len) + FILE_HEADER_FLAGS


def _build_ppth(file_path: str) -> bytes:
    """Build PPTH tag (file path, UTF-16BE)."""
    normalized = file_path.replace("\\", "/")
    path_bytes = normalized.encode("utf-16-be")
    path_len = len(path_bytes)
    tag_len = 16 + path_len
    data = struct.pack(">4sII", b"PPTH", 16, tag_len)
    data += struct.pack(">I", path_len)
    data += path_bytes
    return data


def _build_pvbr() -> bytes:
    """Build PVBR tag (VBR info, 1620 bytes total)."""
    tag_len = 1620
    data = struct.pack(">4sII", b"PVBR", 16, tag_len)
    data += b"\x00" * (tag_len - 12)
    return data


def _build_pqtz(beat_times_sec: list[float], beat_types: list[int], bpms: list[float]) -> bytes:
    """Build PQTZ tag (beat grid)."""
    n = len(beat_times_sec)
    header_len = 24
    tag_len = header_len + (n * 8)

    data = struct.pack(">4sIII", b"PQTZ", header_len, tag_len, 0)
    data += struct.pack(">II", 0x00080000, n)

    for i in range(n):
        beat = beat_types[i] if i < len(beat_types) else ((i % 4) + 1)
        tempo = int(round(bpms[min(i, len(bpms) - 1)] * 100))
        time_ms = int(round(beat_times_sec[i] * 1000))
        data += struct.pack(">hhi", beat, tempo, time_ms)

    return data


def _build_pwav(mono_waveform: list[int], target_entries: int = 400) -> bytes:
    """Build PWAV tag (preview waveform, 400 entries, 1 byte each)."""
    arr = np.array(mono_waveform, dtype=np.float32)
    if len(arr) != target_entries:
        indices = np.linspace(0, len(arr) - 1, target_entries).astype(int)
        arr = arr[indices]
    mx = arr.max() if arr.max() > 0 else 1
    arr = np.clip((arr / mx) * 255, 0, 255).astype(np.uint8)

    tag_len = 20 + target_entries
    data = struct.pack(">4sII", b"PWAV", 20, tag_len)
    data += struct.pack(">II", target_entries, 0x00010000)
    data += arr.tobytes()
    return data


def _build_pwv2(mono_waveform: list[int], target_entries: int = 100) -> bytes:
    """Build PWV2 tag (detail waveform, 100 entries, 1 byte each)."""
    arr = np.array(mono_waveform, dtype=np.float32)
    if len(arr) != target_entries:
        indices = np.linspace(0, len(arr) - 1, target_entries).astype(int)
        arr = arr[indices]
    mx = arr.max() if arr.max() > 0 else 1
    arr = np.clip((arr / mx) * 255, 0, 255).astype(np.uint8)

    tag_len = 20 + target_entries
    data = struct.pack(">4sII", b"PWV2", 20, tag_len)
    data += struct.pack(">II", target_entries, 0x00010000)
    data += arr.tobytes()
    return data


def _build_pwv3(mono_waveform: list[int], target_entries: int = 1600) -> bytes:
    """Build PWV3 tag (preview waveform for EXT, 1600 entries, 1 byte each)."""
    arr = np.array(mono_waveform, dtype=np.float32)
    if len(arr) != target_entries:
        indices = np.linspace(0, len(arr) - 1, target_entries).astype(int)
        arr = arr[indices]
    mx = arr.max() if arr.max() > 0 else 1
    arr = np.clip((arr / mx) * 255, 0, 255).astype(np.uint8)

    tag_len = 24 + target_entries
    data = struct.pack(">4sIII", b"PWV3", 24, tag_len, 1)
    data += struct.pack(">I", target_entries)
    data += struct.pack(">I", 0x00960000)
    data += arr.tobytes()
    return data


def _build_pwv5(rgb_waveform: list[dict], target_entries: int = 1600) -> bytes:
    """Build PWV5 tag (color waveform, 1600 entries, 2 bytes each, 5-bit RGB)."""
    if len(rgb_waveform) != target_entries:
        indices = np.linspace(0, len(rgb_waveform) - 1, target_entries).astype(int)
        rgb_waveform = [rgb_waveform[i] for i in indices]

    tag_len = 24 + (target_entries * 2)
    data = struct.pack(">4sIII", b"PWV5", 24, tag_len, 2)
    data += struct.pack(">I", target_entries)
    data += struct.pack(">I", 0x00960305)

    for entry in rgb_waveform:
        r = min(31, max(0, entry.get("r", 0) >> 3))
        g = min(31, max(0, entry.get("g", 0) >> 3))
        b = min(31, max(0, entry.get("b", 0) >> 3))
        packed = (r << 10) | (g << 5) | b
        data += struct.pack(">H", packed)

    return data


def _build_pcob(cue_type: int) -> bytes:
    """Build PCOB tag (cue container, empty). type=1 hot cues, type=0 memory."""
    data = struct.pack(">4sIII", b"PCOB", 24, 24, cue_type)
    data += struct.pack(">I", 0)
    data += struct.pack(">i", -1)
    return data


def _build_pco2(cue_type: int) -> bytes:
    """Build PCO2 tag (extended cue container, empty)."""
    data = struct.pack(">4sII", b"PCO2", 20, 20)
    data += struct.pack(">II", cue_type, 0)
    return data


# ─── ANLZ File Writer ────────────────────────────────────────────────────────

def _get_anlz_dir(rekordbox_id: str) -> str:
    """Get or create the ANLZ directory for a track."""
    track_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, rekordbox_id))
    subdir = track_uuid[:3]
    anlz_dir = os.path.join(ANLZ_ROOT, subdir, track_uuid)
    os.makedirs(anlz_dir, exist_ok=True)
    return anlz_dir


def write_anlz_files(
    rekordbox_id: str,
    file_path: str,
    analysis: AnalysisResult,
) -> dict:
    """Write CDJ-compatible ANLZ files (.DAT + .EXT)."""
    try:
        anlz_dir = _get_anlz_dir(rekordbox_id)
        dat_path = os.path.join(anlz_dir, "ANLZ0000.DAT")
        ext_path = os.path.join(anlz_dir, "ANLZ0000.EXT")

        bpms = [analysis.bpm] if analysis.bpm else [120.0]

        # DAT: PPTH → PVBR → PQTZ → PWAV → PWV2 → PCOB(hot) → PCOB(memory)
        tags_dat = b"".join([
            _build_ppth(file_path),
            _build_pvbr(),
            _build_pqtz(analysis.beat_grid, analysis.beat_positions, bpms),
            _build_pwav(analysis.waveform_mono),
            _build_pwv2(analysis.waveform_mono),
            _build_pcob(1),
            _build_pcob(0),
        ])
        dat_data = _build_file_header(28 + len(tags_dat)) + tags_dat

        with open(dat_path, "wb") as f:
            f.write(dat_data)
        logger.info("Wrote DAT: %s (%d bytes, %d beats)", dat_path, len(dat_data), len(analysis.beat_grid))

        # EXT: PPTH → PWV3 → PCOB(hot) → PCOB(memory) → PCO2(hot) → PCO2(memory) → PWV5
        tags_ext = b"".join([
            _build_ppth(file_path),
            _build_pwv3(analysis.waveform_mono),
            _build_pcob(1),
            _build_pcob(0),
            _build_pco2(1),
            _build_pco2(0),
            _build_pwv5(analysis.waveform_rgb),
        ])
        ext_data = _build_file_header(28 + len(tags_ext)) + tags_ext

        with open(ext_path, "wb") as f:
            f.write(ext_data)
        logger.info("Wrote EXT: %s (%d bytes)", ext_path, len(ext_data))

        return {"success": True, "dat_path": dat_path, "ext_path": ext_path, "anlz_dir": anlz_dir}

    except Exception as e:
        logger.error("Failed to write ANLZ for track %s: %s", rekordbox_id, e)
        return {"error": str(e)}


# ─── Rekordbox DB Import ─────────────────────────────────────────────────────

def import_track_unanalyzed(file_path: str, track: TrackInfo) -> dict:
    """Import a track into Rekordbox DB WITHOUT analysis. Rekordbox will analyze it on open."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables
        import random
        from sqlalchemy import func as sa_func

        db = Rekordbox6Database()

        # Check if already exists
        normalized = file_path.replace("\\", "/")
        existing = db.session.query(tables.DjmdContent).filter_by(FolderPath=normalized).first()
        if existing:
            logger.info("Track already in Rekordbox: %s (ID=%s)", file_path, existing.ID)
            return {"status": "exists", "id": str(existing.ID)}

        # Generate unique ID
        new_id = str(random.randint(100000000, 999999999))
        while db.session.query(tables.DjmdContent).filter_by(ID=new_id).first():
            new_id = str(random.randint(100000000, 999999999))

        content = tables.DjmdContent()
        content.ID = new_id
        content.FolderPath = normalized
        content.Title = track.title
        content.FileNameL = Path(file_path).name
        content.FileSize = Path(file_path).stat().st_size if Path(file_path).exists() else 0
        content.FileType = 1      # MP3
        content.BitRate = 320
        content.SampleRate = 44100
        content.Analysed = 0      # NOT analyzed — Rekordbox will do it
        content.rb_data_status = 0
        content.rb_local_data_status = 0
        content.rb_local_deleted = 0
        content.rb_local_synced = 0
        content.usn = 0
        max_usn = db.session.query(sa_func.max(tables.DjmdContent.rb_local_usn)).scalar() or 0
        content.rb_local_usn = max_usn + 1
        content.created_at = datetime.now(timezone.utc)
        content.updated_at = datetime.now(timezone.utc)

        # Lookup existing artist (don't create new)
        try:
            artist_obj = db.session.query(tables.DjmdArtist).filter_by(Name=track.artist).first()
            if artist_obj:
                content.ArtistID = artist_obj.ID
            else:
                content.Title = f"{track.artist} - {track.title}"
        except Exception:
            pass

        db.session.add(content)
        db.session.commit()

        logger.info("Imported to Rekordbox (unanalyzed): %s (ID=%s)", track.title, new_id)
        return {"status": "imported", "id": new_id}

    except Exception as e:
        logger.error("Rekordbox import failed for %s: %s", file_path, e)
        return {"status": "error", "error": str(e)}


def import_track(file_path: str, track: TrackInfo, analysis: AnalysisResult) -> dict:
    """Import a new track into Rekordbox master.db and write ANLZ files."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()

        # Check if track already exists by file path
        normalized = file_path.replace("\\", "/")
        existing = db.session.query(tables.DjmdContent).filter_by(FolderPath=normalized).first()
        if existing:
            logger.info("Track already in Rekordbox: %s (ID=%s)", file_path, existing.ID)
            # Update analysis
            _update_content_analysis(existing, analysis, db)
            anlz_result = write_anlz_files(str(existing.ID), file_path, analysis)
            db.session.commit()
            return {"status": "updated", "id": str(existing.ID), "anlz": anlz_result}

        # Create new DjmdContent — set attributes individually (NOT kwargs)
        # Generate unique ID (9-digit string, matching Rekordbox format)
        import random
        new_id = str(random.randint(100000000, 999999999))
        # Ensure unique
        while db.session.query(tables.DjmdContent).filter_by(ID=new_id).first():
            new_id = str(random.randint(100000000, 999999999))

        content = tables.DjmdContent()
        content.ID = new_id
        content.FolderPath = normalized
        content.Title = track.title
        content.FileNameL = Path(file_path).name
        content.FileSize = Path(file_path).stat().st_size if Path(file_path).exists() else 0
        content.BPM = int(round(analysis.bpm * 100))
        content.BitRate = 320
        content.SampleRate = 44100
        content.FileType = 1  # 1 = MP3
        content.Analysed = 105
        content.rb_data_status = 0
        content.rb_local_data_status = 0
        content.rb_local_deleted = 0
        content.rb_local_synced = 0
        content.usn = 0
        # Get next rb_local_usn
        from sqlalchemy import func as sa_func
        max_usn = db.session.query(sa_func.max(tables.DjmdContent.rb_local_usn)).scalar() or 0
        content.rb_local_usn = max_usn + 1
        content.created_at = datetime.now(timezone.utc)
        content.updated_at = datetime.now(timezone.utc)

        # Set artist — find existing only, don't create new (avoids NULL ID crash)
        try:
            artist_obj = db.session.query(tables.DjmdArtist).filter_by(Name=track.artist).first()
            if artist_obj:
                content.ArtistID = artist_obj.ID
            else:
                # Store artist name in Title as "Artist - Title" for display
                content.Title = f"{track.artist} - {track.title}"
        except Exception as e:
            logger.warning("Failed to lookup artist: %s", e)

        # Set key
        if analysis.key_camelot:
            key_obj = db.session.query(tables.DjmdKey).filter_by(ScaleName=analysis.key_camelot).first()
            if key_obj:
                content.KeyID = key_obj.ID

        # Set album — find existing only, don't create new
        try:
            if track.album:
                album_obj = db.session.query(tables.DjmdAlbum).filter_by(Name=track.album).first()
                if album_obj:
                    content.AlbumID = album_obj.ID
        except Exception as e:
            logger.warning("Failed to lookup album: %s", e)

        db.session.add(content)
        db.session.flush()

        rekordbox_id = str(content.ID)

        # Write ANLZ files
        anlz_result = write_anlz_files(rekordbox_id, file_path, analysis)

        # Set analysis data path
        if anlz_result.get("success"):
            share_root = os.path.join(os.environ.get("APPDATA", ""), "Pioneer", "rekordbox", "share")
            dat_path = os.path.join(anlz_result["anlz_dir"], "ANLZ0000.DAT")
            rel = os.path.relpath(dat_path, share_root).replace("\\", "/")
            content.AnalysisDataPath = "/" + rel

        content.updated_at = datetime.now(timezone.utc)
        db.session.commit()

        logger.info("Imported to Rekordbox: %s (ID=%s, BPM=%.1f, Key=%s)",
                     track.title, rekordbox_id, analysis.bpm, analysis.key_camelot)
        return {"status": "imported", "id": rekordbox_id, "anlz": anlz_result}

    except Exception as e:
        logger.error("Rekordbox import failed for %s: %s", file_path, e)
        return {"status": "error", "error": str(e)}


def _update_content_analysis(content, analysis: AnalysisResult, db) -> None:
    """Update analysis metadata on an existing DjmdContent."""
    from pyrekordbox.db6 import tables

    content.BPM = int(round(analysis.bpm * 100))
    content.Analysed = 105
    if analysis.key_camelot:
        key_obj = db.session.query(tables.DjmdKey).filter_by(ScaleName=analysis.key_camelot).first()
        if key_obj:
            content.KeyID = key_obj.ID
    content.updated_at = datetime.now(timezone.utc)


# ─── Rekordbox Playlist Management ──────────────────────────────────────────

def find_or_create_playlist(playlist_name: str) -> str | None:
    """Find existing or create new Rekordbox playlist. Returns playlist ID."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()

        # Find existing
        for pl in db.session.query(tables.DjmdPlaylist).all():
            if pl.Name == playlist_name:
                return str(pl.ID)

        # Create new at top (Seq=0, bump others)
        for pl in db.session.query(tables.DjmdPlaylist).all():
            if pl.Seq is not None:
                pl.Seq = (pl.Seq or 0) + 1

        playlist = tables.DjmdPlaylist()
        playlist.ID = str(abs(hash(f'sff_{playlist_name}')) % (10 ** 10))
        playlist.Name = playlist_name
        playlist.Seq = 0
        playlist.Attribute = 0
        playlist.ParentID = 'root'
        playlist.rb_data_status = 1
        playlist.rb_local_data_status = 0
        playlist.rb_local_deleted = 0
        playlist.rb_local_synced = 0
        playlist.usn = 0
        playlist.rb_local_usn = 0
        playlist.created_at = datetime.now(timezone.utc)
        playlist.updated_at = datetime.now(timezone.utc)
        db.session.add(playlist)
        db.session.commit()

        logger.info("Created Rekordbox playlist: %s (Seq=0, top)", playlist_name)
        return str(playlist.ID)

    except Exception as e:
        logger.error("Failed to find/create Rekordbox playlist '%s': %s", playlist_name, e)
        return None


def add_track_to_playlist(playlist_id: str, content_id: str, track_no: int) -> bool:
    """Add a track to a Rekordbox playlist at the given position."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()

        # Check if already in playlist
        existing = db.session.query(tables.DjmdSongPlaylist).filter_by(
            PlaylistID=playlist_id, ContentID=content_id,
        ).first()
        if existing:
            existing.TrackNo = track_no
            existing.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            return True

        song = tables.DjmdSongPlaylist()
        song.ID = str(abs(hash(f'sff_{playlist_id}_{content_id}_{track_no}')) % (10 ** 10))
        song.PlaylistID = playlist_id
        song.ContentID = content_id
        song.TrackNo = track_no
        song.rb_data_status = 1
        song.rb_local_data_status = 0
        song.rb_local_deleted = 0
        song.rb_local_synced = 0
        song.usn = 0
        song.rb_local_usn = 0
        song.created_at = datetime.now(timezone.utc)
        song.updated_at = datetime.now(timezone.utc)
        db.session.add(song)
        db.session.commit()
        return True

    except Exception as e:
        logger.error("Failed to add track to playlist: %s", e)
        return False


def sync_playlist_order(playlist_name: str, filenames: list[str]) -> str:
    """Reorder tracks in Rekordbox playlist to match filename order."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()

        # Find playlist
        playlist = None
        for pl in db.session.query(tables.DjmdPlaylist).all():
            if pl.Name == playlist_name:
                playlist = pl
                break

        if not playlist:
            return "playlist not found"

        # Get songs
        songs = db.session.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=playlist.ID).all()
        if not songs:
            return "no songs in playlist"

        # Build filename -> song map
        song_map = {}
        for song in songs:
            content = db.session.query(tables.DjmdContent).filter_by(ID=song.ContentID).first()
            if content:
                fp = str(getattr(content, 'FolderPath', ''))
                filename = Path(fp).name
                song_map[filename] = song

        # Reorder
        reordered = 0
        for i, filename in enumerate(filenames):
            if filename in song_map:
                song = song_map[filename]
                new_track_no = i + 1
                if song.TrackNo != new_track_no:
                    song.TrackNo = new_track_no
                    song.updated_at = datetime.now(timezone.utc)
                    reordered += 1

        db.session.commit()
        return f"reordered {reordered} tracks"

    except Exception as e:
        logger.error("Rekordbox playlist sync failed: %s", e)
        return f"error: {e}"


def remove_track_from_playlist(playlist_name: str, filename: str) -> bool:
    """Remove a track from a Rekordbox playlist (does NOT delete the track from library)."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()

        # Find playlist
        playlist = None
        for pl in db.session.query(tables.DjmdPlaylist).all():
            if pl.Name == playlist_name:
                playlist = pl
                break
        if not playlist:
            return False

        songs = db.session.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=playlist.ID).all()
        for song in songs:
            content = db.session.query(tables.DjmdContent).filter_by(ID=song.ContentID).first()
            if content:
                fp = str(getattr(content, 'FolderPath', ''))
                if Path(fp).name == filename:
                    db.session.delete(song)
                    db.session.commit()
                    logger.info("Removed '%s' from Rekordbox playlist '%s'", filename, playlist_name)
                    return True

        return False

    except Exception as e:
        logger.error("Failed to remove track from Rekordbox playlist: %s", e)
        return False


def find_content_by_path(file_path: str) -> str | None:
    """Find Rekordbox content ID by file path."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()
        normalized = file_path.replace("\\", "/")
        content = db.session.query(tables.DjmdContent).filter_by(FolderPath=normalized).first()
        if content:
            return str(content.ID)
        return None
    except Exception:
        return None


def find_content_by_title(artist: str, title: str) -> tuple[str, str] | None:
    """Find Rekordbox content by artist+title search. Returns (content_id, file_path) or None.
    MUST match BOTH artist AND title to avoid false positives (e.g. 'Dreamer' by different artists)."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()

        title_lower = title.strip().lower()
        artist_lower = artist.strip().lower()
        first_artist = artist_lower.split(",")[0].strip()

        # Strategy 1: Match by filename pattern "Artist - Title" (most reliable)
        target_name = f"{artist} - {title}".lower()
        for content in db.session.query(tables.DjmdContent).all():
            fp = str(getattr(content, 'FolderPath', '') or '')
            fname = Path(fp).stem.lower()
            if fname == target_name:
                logger.info("Found in Rekordbox by exact filename: '%s' (ID=%s)", target_name, content.ID)
                return (str(content.ID), fp)

        # Strategy 2: Match by Title + verify artist is in the filename or path
        for content in db.session.query(tables.DjmdContent).all():
            ct = str(getattr(content, 'Title', '') or '').strip().lower()
            if ct == title_lower:
                fp = str(getattr(content, 'FolderPath', '') or '')
                fp_lower = fp.lower()
                # Artist MUST appear in the file path
                if first_artist in fp_lower:
                    logger.info("Found in Rekordbox by title+artist: '%s' by '%s' (ID=%s)", title, artist, content.ID)
                    return (str(content.ID), fp)
                # Also check ArtistID
                if getattr(content, 'ArtistID', None):
                    artist_obj = db.session.query(tables.DjmdArtist).filter_by(ID=content.ArtistID).first()
                    if artist_obj and first_artist in str(artist_obj.Name or '').lower():
                        logger.info("Found in Rekordbox by title+ArtistID: '%s' by '%s' (ID=%s)", title, artist, content.ID)
                        return (str(content.ID), fp)

        # Strategy 3: Partial filename match — artist AND title both in filename
        for content in db.session.query(tables.DjmdContent).all():
            fp = str(getattr(content, 'FolderPath', '') or '')
            fname = Path(fp).stem.lower()
            if title_lower in fname and first_artist in fname:
                logger.info("Found in Rekordbox by partial match: '%s' by '%s' in '%s' (ID=%s)", title, first_artist, fname, content.ID)
                return (str(content.ID), fp)

        return None
    except Exception as e:
        logger.warning("Rekordbox title search failed: %s", e)
        return None


def flush_wal():
    """Checkpoint the WAL file into master.db so Rekordbox can see our changes."""
    try:
        from pyrekordbox import Rekordbox6Database
        from sqlalchemy import text

        db = Rekordbox6Database()
        db.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        db.session.commit()
        db.session.close()
        db.engine.dispose()
        logger.info("WAL checkpoint complete — changes flushed to master.db")
    except Exception as e:
        logger.error("WAL checkpoint failed: %s", e)
