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
from services.platform_paths import (
    DEFAULT_ANLZ_ROOT,
    REKORDBOX_BASE_DIR,
    REKORDBOX_PLAYLISTS_XML,
    REKORDBOX_SHARE_DIR,
    REKORDBOX_WAL,
)

logger = logging.getLogger(__name__)

ANLZ_ROOT = os.getenv("ANLZ_ROOT", DEFAULT_ANLZ_ROOT)

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

def _get_or_create_artist(db, tables, name: str) -> str | None:
    """Look up an artist by name, or create a fresh DjmdArtist row with explicit ID.
    Returns the artist ID. Without this, Rekordbox batch analysis hangs on the 2nd track."""
    if not name:
        return None
    try:
        existing = db.session.query(tables.DjmdArtist).filter_by(Name=name).first()
        if existing:
            return str(existing.ID)

        import random
        from sqlalchemy import func as sa_func

        new_id = str(random.randint(1000000000, 9999999999))
        while db.session.query(tables.DjmdArtist).filter_by(ID=new_id).first():
            new_id = str(random.randint(1000000000, 9999999999))

        import uuid as _uuid
        artist = tables.DjmdArtist()
        artist.ID = new_id
        artist.UUID = str(_uuid.uuid4())
        artist.Name = name
        artist.rb_data_status = 0
        artist.rb_local_data_status = 0
        artist.rb_local_deleted = 0
        artist.rb_local_synced = 0
        artist.usn = None
        max_usn = db.session.query(sa_func.max(tables.DjmdArtist.rb_local_usn)).scalar() or 0
        artist.rb_local_usn = max_usn + 1
        artist.created_at = datetime.now(timezone.utc)
        artist.updated_at = datetime.now(timezone.utc)
        db.session.add(artist)
        db.session.flush()
        logger.info("Created DjmdArtist: %s (ID=%s)", name, new_id)
        return new_id
    except Exception as e:
        logger.warning("Failed to create artist '%s': %s", name, e)
        return None


def _get_or_create_album(db, tables, name: str, artist_id: str | None = None) -> str | None:
    """Look up an album by name, or create a fresh DjmdAlbum row with explicit ID."""
    if not name:
        return None
    try:
        existing = db.session.query(tables.DjmdAlbum).filter_by(Name=name).first()
        if existing:
            return str(existing.ID)

        import random
        from sqlalchemy import func as sa_func

        new_id = str(random.randint(1000000000, 9999999999))
        while db.session.query(tables.DjmdAlbum).filter_by(ID=new_id).first():
            new_id = str(random.randint(1000000000, 9999999999))

        import uuid as _uuid
        album = tables.DjmdAlbum()
        album.ID = new_id
        album.UUID = str(_uuid.uuid4())
        album.Name = name
        if artist_id:
            album.AlbumArtistID = artist_id
        album.rb_data_status = 0
        album.rb_local_data_status = 0
        album.rb_local_deleted = 0
        album.rb_local_synced = 0
        album.usn = None
        max_usn = db.session.query(sa_func.max(tables.DjmdAlbum.rb_local_usn)).scalar() or 0
        album.rb_local_usn = max_usn + 1
        album.created_at = datetime.now(timezone.utc)
        album.updated_at = datetime.now(timezone.utc)
        db.session.add(album)
        db.session.flush()
        logger.info("Created DjmdAlbum: %s (ID=%s)", name, new_id)
        return new_id
    except Exception as e:
        logger.warning("Failed to create album '%s': %s", name, e)
        return None


_FILE_TYPES = {".mp3": 1, ".m4a": 4, ".flac": 5, ".wav": 11, ".aiff": 12, ".aif": 12}


def _audio_properties(file_path: str) -> tuple[int, int, int]:
    """(Rekordbox FileType, bitrate in kbps, sample rate) read from the file
    itself. These used to be hard-coded (MP3 / 320kbps / 44.1kHz): a wrong sample
    rate made Rekordbox's batch analysis hang on 48kHz yt-dlp output, and a wrong
    FileType mislabels FLAC/AIFF/WAV imports as MP3."""
    file_type = _FILE_TYPES.get(Path(file_path).suffix.lower(), 1)
    sample_rate, bitrate = 44100, 320
    try:
        import mutagen
        info = mutagen.File(file_path).info
        sample_rate = int(info.sample_rate) or sample_rate
        if getattr(info, "bitrate", None):
            bitrate = int(info.bitrate) // 1000  # mutagen returns bps
    except Exception:
        pass
    return file_type, bitrate, sample_rate


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

        import uuid as _uuid
        today = datetime.now().strftime('%Y-%m-%d')

        file_type, actual_bitrate, actual_sr = _audio_properties(file_path)

        content = tables.DjmdContent()
        content.ID = new_id
        content.UUID = str(_uuid.uuid4())  # CRITICAL: Rekordbox uses UUID to build ANLZ paths
        content.FolderPath = normalized
        content.Title = track.title
        content.FileNameL = Path(file_path).name
        content.FileSize = Path(file_path).stat().st_size if Path(file_path).exists() else 0
        content.FileType = file_type
        content.BitRate = actual_bitrate
        content.SampleRate = actual_sr
        content.Analysed = 0      # NOT analyzed — Rekordbox will do it
        # Drag-import parity fields (without these, batch analysis hangs)
        content.HotCueAutoLoad = 'on'
        content.DeliveryControl = 'on'
        content.StockDate = today
        content.DateCreated = today
        content.ColorID = '0'
        content.DJPlayCount = 0
        content.DiscNo = 0
        content.Rating = 0
        content.TrackNo = 0
        content.rb_data_status = 0
        content.rb_local_data_status = 0
        content.rb_local_deleted = 0
        content.rb_local_synced = 0
        content.usn = 0
        max_usn = db.session.query(sa_func.max(tables.DjmdContent.rb_local_usn)).scalar() or 0
        content.rb_local_usn = max_usn + 1
        content.created_at = datetime.now(timezone.utc)
        content.updated_at = datetime.now(timezone.utc)

        # Drag-import parity: create proper Artist + Album rows so Rekordbox can analyze
        # in batch without hanging. AlbumID=None / ArtistID=None causes the 2nd-track-hang.
        artist_id = _get_or_create_artist(db, tables, track.artist)
        if artist_id:
            content.ArtistID = artist_id
        album_id = _get_or_create_album(db, tables, track.album, artist_id=artist_id)
        if album_id:
            content.AlbumID = album_id

        db.session.add(content)
        db.session.commit()

        logger.info("Imported to Rekordbox (unanalyzed): %s (ID=%s, UUID=%s, ArtistID=%s, AlbumID=%s)",
                    track.title, new_id, content.UUID, artist_id, album_id)
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

        # Read actual SampleRate/BitRate from file (yt-dlp output is 48000Hz, hardcoding 44100 caused hangs)
        actual_sr = 44100
        actual_bitrate = 320
        try:
            from mutagen.mp3 import MP3
            audio = MP3(file_path)
            actual_sr = audio.info.sample_rate
            if audio.info.bitrate:
                actual_bitrate = audio.info.bitrate // 1000
        except Exception:
            pass

        import uuid as _uuid
        content = tables.DjmdContent()
        content.ID = new_id
        content.UUID = str(_uuid.uuid4())  # CRITICAL for ANLZ path
        content.FolderPath = normalized
        content.Title = track.title
        content.FileNameL = Path(file_path).name
        content.FileSize = Path(file_path).stat().st_size if Path(file_path).exists() else 0
        content.BPM = int(round(analysis.bpm * 100))
        content.BitRate = actual_bitrate
        content.SampleRate = actual_sr
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

        # Drag-import parity: create proper Artist + Album rows
        artist_id = _get_or_create_artist(db, tables, track.artist)
        if artist_id:
            content.ArtistID = artist_id
        album_id = _get_or_create_album(db, tables, track.album, artist_id=artist_id)
        if album_id:
            content.AlbumID = album_id

        # Set key
        if analysis.key_camelot:
            key_obj = db.session.query(tables.DjmdKey).filter_by(ScaleName=analysis.key_camelot).first()
            if key_obj:
                content.KeyID = key_obj.ID

        db.session.add(content)
        db.session.flush()

        rekordbox_id = str(content.ID)

        # Write ANLZ files
        anlz_result = write_anlz_files(rekordbox_id, file_path, analysis)

        # Set analysis data path
        if anlz_result.get("success"):
            dat_path = os.path.join(anlz_result["anlz_dir"], "ANLZ0000.DAT")
            rel = os.path.relpath(dat_path, REKORDBOX_SHARE_DIR).replace("\\", "/")
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
    """Find existing or create new Rekordbox playlist. Returns playlist ID.

    CRITICAL: New playlists must:
    1. Have UUID set (otherwise appear empty in Rekordbox UI)
    2. Have a 32-bit ID (≤ 2^32 - 1) — Rekordbox stores playlist IDs as 32-bit
       integers in masterPlaylists6.xml. IDs > 32-bit produce 9-hex-char entries
       which Rekordbox can't read, so the playlist is hidden.
    3. Be registered in masterPlaylists6.xml as a NODE entry. Without that,
       Rekordbox doesn't know the playlist exists even if it's in master.db.
    """
    try:
        import random
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

        # 32-bit ID (must fit in unsigned 32-bit so hex is ≤ 8 chars)
        new_id = str(random.randint(1, (2**32) - 1))
        while db.session.query(tables.DjmdPlaylist).filter_by(ID=new_id).first():
            new_id = str(random.randint(1, (2**32) - 1))

        import uuid as _uuid
        playlist = tables.DjmdPlaylist()
        playlist.ID = new_id
        playlist.UUID = str(_uuid.uuid4())  # CRITICAL: without UUID, playlist appears empty in Rekordbox UI
        playlist.Name = playlist_name
        playlist.Seq = 0
        playlist.Attribute = 0
        playlist.ParentID = 'root'
        playlist.rb_data_status = 0
        playlist.rb_local_data_status = 0
        playlist.rb_local_deleted = 0
        playlist.rb_local_synced = 0
        playlist.usn = None
        from sqlalchemy import func as sa_func
        max_usn = db.session.query(sa_func.max(tables.DjmdPlaylist.rb_local_usn)).scalar() or 0
        playlist.rb_local_usn = max_usn + 1
        playlist.created_at = datetime.now(timezone.utc)
        playlist.updated_at = datetime.now(timezone.utc)
        db.session.add(playlist)
        db.session.commit()

        # Register in masterPlaylists6.xml — without this, Rekordbox doesn't
        # know the playlist exists even though it's in master.db
        _register_playlist_in_xml(new_id)

        logger.info("Created Rekordbox playlist: %s (Seq=0, top, ID=%s)", playlist_name, new_id)
        return new_id

    except Exception as e:
        logger.error("Failed to find/create Rekordbox playlist '%s': %s", playlist_name, e)
        return None


def _register_playlist_in_xml(playlist_id: str):
    """Add a NODE entry to masterPlaylists6.xml for a new playlist.
    Rekordbox uses this XML as a registry of which playlists exist.
    Without an entry here, Rekordbox shows the playlist as empty in the UI."""
    try:
        import os
        import xml.etree.ElementTree as ET
        from datetime import datetime as _dt

        xml_path = REKORDBOX_PLAYLISTS_XML
        if not os.path.exists(xml_path):
            logger.warning("masterPlaylists6.xml not found at %s — playlist may appear empty in UI", xml_path)
            return

        # Encode playlist ID as uppercase hex (without 0x prefix)
        id_hex = hex(int(playlist_id))[2:].upper()
        timestamp_ms = int(_dt.now().timestamp() * 1000)

        tree = ET.parse(xml_path)
        root = tree.getroot()
        playlists_node = root.find("PLAYLISTS")
        if playlists_node is None:
            logger.warning("PLAYLISTS node not found in masterPlaylists6.xml")
            return

        # Skip if already registered
        for n in playlists_node.findall("NODE"):
            if n.get("Id") == id_hex:
                return

        ET.SubElement(playlists_node, "NODE", {
            "Id": id_hex,
            "ParentId": "0",
            "Attribute": "0",
            "Timestamp": str(timestamp_ms),
            "Lib_Type": "0",
            "CheckType": "0",
        })
        tree.write(xml_path, encoding="UTF-8", xml_declaration=True)
        logger.info("Registered playlist ID %s (hex %s) in masterPlaylists6.xml", playlist_id, id_hex)

    except Exception as e:
        logger.error("Failed to register playlist in masterPlaylists6.xml: %s", e)


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

        import uuid as _uuid
        from sqlalchemy import func as sa_func

        song = tables.DjmdSongPlaylist()
        song.ID = str(abs(hash(f'sff_{playlist_id}_{content_id}_{track_no}')) % (10 ** 10))
        song.UUID = str(_uuid.uuid4())  # CRITICAL: Rekordbox filters out song rows without UUID
        song.PlaylistID = playlist_id
        song.ContentID = content_id
        song.TrackNo = track_no
        song.rb_data_status = 0
        song.rb_local_data_status = 0
        song.rb_local_deleted = 0
        song.rb_local_synced = 0
        song.usn = None
        max_usn = db.session.query(sa_func.max(tables.DjmdSongPlaylist.rb_local_usn)).scalar() or 0
        song.rb_local_usn = max_usn + 1
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


def find_playlist_id(playlist_name: str) -> str | None:
    """Read-only lookup of a playlist's ID by name. Unlike find_or_create_playlist
    this never writes, so it's safe to call before the pipeline is ready to touch
    Rekordbox. An exact match wins; otherwise a case-insensitive one is accepted,
    because a playlist made by hand ("Deep Tech AIFF") shouldn't be duplicated just
    because Spotify's name for it is "Deep tech"."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()
        exact = insensitive = None
        for pl in db.session.query(tables.DjmdPlaylist).all():
            name = pl.Name or ""
            if name == playlist_name:
                exact = str(pl.ID)
                break
            if insensitive is None and name.lower() == playlist_name.lower():
                insensitive = str(pl.ID)
        db.session.close()
        db.engine.dispose()
        return exact or insensitive
    except Exception as e:
        logger.warning("Failed to look up playlist '%s': %s", playlist_name, e)
        return None


def get_playlist_name(playlist_id: str) -> str | None:
    """A playlist's current name, or None if it no longer exists (deleted or
    renamed away in Rekordbox) -- so a stored ID can be trusted only if it still
    resolves."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()
        pl = db.session.query(tables.DjmdPlaylist).filter_by(ID=str(playlist_id)).first()
        name = pl.Name if pl else None
        db.session.close()
        db.engine.dispose()
        return name
    except Exception as e:
        logger.warning("Failed to read playlist %s: %s", playlist_id, e)
        return None


def get_playlist_track_paths(playlist_id: str) -> dict[str, str]:
    """{title: file path} for every track currently in a Rekordbox playlist. Reads
    the playlist itself, so each output format's state comes from ITS OWN playlist
    -- a library-wide title search could return the FLAC copy's path when asked
    about the AIFF one."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()
        songs = db.session.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=playlist_id).all()
        out = {}
        for s in songs:
            c = db.session.query(tables.DjmdContent).filter_by(ID=s.ContentID).first()
            if c and c.Title:
                out[c.Title] = c.FolderPath or ""
        db.session.close()
        db.engine.dispose()
        return out
    except Exception as e:
        logger.warning("Failed to read playlist tracks for '%s': %s", playlist_id, e)
        return {}


def get_library_files() -> list[tuple[str, str]]:
    """(title, file path) for every track in the Rekordbox library, in ONE read --
    for callers that must check many tracks against the whole library (per-track
    lookups would each rescan it)."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()
        out = [(str(c.Title or ""), str(c.FolderPath or "")) for c in db.session.query(tables.DjmdContent).all()]
        db.session.close()
        db.engine.dispose()
        return out
    except Exception as e:
        logger.warning("Failed to read the Rekordbox library: %s", e)
        return []


def get_playlist_track_titles(playlist_id: str) -> set[str]:
    """Titles of every track currently in a Rekordbox playlist -- the ground
    truth for "is this track already done", since it reads the durable
    database directly rather than an ephemeral download-history log that can
    go stale/get pruned."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()
        songs = db.session.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=playlist_id).all()
        titles = set()
        for s in songs:
            c = db.session.query(tables.DjmdContent).filter_by(ID=s.ContentID).first()
            if c and c.Title:
                titles.add(c.Title)
        db.session.close()
        db.engine.dispose()
        return titles
    except Exception as e:
        logger.warning("Failed to read playlist titles for '%s': %s", playlist_id, e)
        return set()


def reorder_playlist_by_titles(playlist_id: str, ordered_titles: list[str]) -> int:
    """Renumber every track currently in the playlist to match `ordered_titles`
    (titles not present are skipped, not treated as gaps). Re-derives the
    FULL ordering from scratch every call -- a track that arrives late (it took
    longer to download) but belongs earlier in the true order will correctly
    push everything after it down, instead of leaving a stale TrackNo that
    collides with whichever track claimed that slot first."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables
        from datetime import datetime, timezone
        from services.labels import title_key

        db = Rekordbox6Database()
        songs = db.session.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=playlist_id).all()
        song_by_title, labelled = {}, {}
        for s in songs:
            c = db.session.query(tables.DjmdContent).filter_by(ID=s.ContentID).first()
            if c:
                key = title_key(c.Title)
                is_labelled = (c.Title or "").strip() != key
                # a stand-in ("Title [label]") and the real "Title" share a key: the real one takes the position
                if key not in song_by_title or (labelled[key] and not is_labelled):
                    song_by_title[key], labelled[key] = s, is_labelled

        reordered = 0
        pos = 1
        for title in ordered_titles:
            song = song_by_title.get(title_key(title))
            if not song:
                continue
            if song.TrackNo != pos:
                song.TrackNo = pos
                song.updated_at = datetime.now(timezone.utc)
                reordered += 1
            pos += 1
        # Tracks that are not in the Spotify list (a different song kept on purpose) go after it, in
        # their existing order, so they can never collide with a number handed out above.
        wanted = {id(song_by_title[k]) for k in (title_key(t) for t in ordered_titles) if k in song_by_title}
        for song in sorted((s for s in songs if id(s) not in wanted), key=lambda s: s.TrackNo or 0):
            if song.TrackNo != pos:
                song.TrackNo = pos
                song.updated_at = datetime.now(timezone.utc)
                reordered += 1
            pos += 1
        db.session.commit()
        db.session.close()
        db.engine.dispose()
        return reordered
    except Exception as e:
        logger.error("Failed to reorder playlist '%s': %s", playlist_id, e)
        return 0


def update_content_path(old_path: str, new_path: str) -> bool:
    """Tell Rekordbox a track's file has moved (same track, new location). Rekordbox must be closed."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()
        content = db.session.query(tables.DjmdContent).filter_by(FolderPath=old_path.replace("\\", "/")).first()
        if not content:
            db.session.close()
            db.engine.dispose()
            return False
        content.FolderPath = new_path.replace("\\", "/")
        content.FileNameL = Path(new_path).name
        content.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        db.session.close()
        db.engine.dispose()
        return True
    except Exception as e:
        logger.error("Failed to update the path for '%s': %s", old_path, e)
        return False


def set_title_by_path(file_path: str, new_title: str, new_artist: str | None = None) -> bool:
    """Change the title (and, if given, the artist) Rekordbox shows for the track stored at `file_path`.
    Rekordbox must be closed."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        db = Rekordbox6Database()
        content = db.session.query(tables.DjmdContent).filter_by(FolderPath=file_path.replace("\\", "/")).first()
        if not content:
            db.session.close()
            db.engine.dispose()
            return False
        content.Title = new_title
        if new_artist:
            content.ArtistID = _get_or_create_artist(db, tables, new_artist)
        content.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        db.session.close()
        db.engine.dispose()
        return True
    except Exception as e:
        logger.error("Failed to set the title for '%s': %s", file_path, e)
        return False


def flush_wal():
    """Checkpoint the WAL file into master.db so Rekordbox can see our changes.
    Runs checkpoint twice to ensure all writes are flushed — pyrekordbox opens
    new connections per call, each creating WAL entries."""
    try:
        from pyrekordbox import Rekordbox6Database
        from sqlalchemy import text
        import os

        # First pass: flush everything written so far
        db = Rekordbox6Database()
        db.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        db.session.commit()
        db.session.close()
        db.engine.dispose()

        # Second pass: the first checkpoint itself may have created WAL entries
        db2 = Rekordbox6Database()
        db2.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        db2.session.commit()
        db2.session.close()
        db2.engine.dispose()

        # Verify WAL is actually empty
        wal_size = os.path.getsize(REKORDBOX_WAL) if os.path.exists(REKORDBOX_WAL) else 0
        logger.info("WAL checkpoint complete — WAL size: %d bytes", wal_size)
    except Exception as e:
        logger.error("WAL checkpoint failed: %s", e)
