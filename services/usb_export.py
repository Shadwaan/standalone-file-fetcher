"""
USB export service.

Exports FF playlists from Rekordbox master.db to USB pen drive.
Copies audio files, ANLZ waveform data, and writes Rekordbox XML
for CDJ import. Uses Rekordbox's own analysis data (not librosa).
"""

import logging
import os
import shutil
import struct
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ANLZ_ROOT = os.getenv(
    "ANLZ_ROOT",
    os.path.join(os.environ.get("APPDATA", ""), "Pioneer", "rekordbox", "share", "PIONEER", "USBANLZ"),
)


@dataclass
class USBExportProgress:
    status: str = "idle"  # idle, running, done, error
    message: str = ""
    phase: str = ""
    playlists_total: int = 0
    playlists_exported: int = 0
    tracks_total: int = 0
    tracks_copied: int = 0
    tracks_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    drive: str = ""
    started_at: str = ""
    finished_at: str = ""


_progress = USBExportProgress()


def get_progress() -> dict:
    return {
        "status": _progress.status,
        "message": _progress.message,
        "phase": _progress.phase,
        "playlists_total": _progress.playlists_total,
        "playlists_exported": _progress.playlists_exported,
        "tracks_total": _progress.tracks_total,
        "tracks_copied": _progress.tracks_copied,
        "tracks_skipped": _progress.tracks_skipped,
        "errors": _progress.errors,
        "drive": _progress.drive,
        "started_at": _progress.started_at,
        "finished_at": _progress.finished_at,
    }


def export_to_usb(drive_letter: str) -> dict:
    """
    Export all FF playlists to a USB drive.

    Flow:
    1. Read FF playlists + tracks from Rekordbox master.db
    2. For each playlist:
       a. Create Contents/{PlaylistName}/ on USB
       b. Copy audio files (skip if already on USB)
       c. Copy ANLZ files from local USBANLZ (rewrite PPTH paths for USB)
    3. Write Rekordbox XML with all playlists + tracks
    """
    global _progress
    _progress = USBExportProgress(
        status="running",
        drive=drive_letter,
        started_at=datetime.now().isoformat(),
    )

    try:
        return _do_export(drive_letter)
    except Exception as e:
        logger.error("USB export failed: %s", e)
        _progress.status = "error"
        _progress.message = str(e)
        _progress.errors.append(str(e))
        return get_progress()
    finally:
        _progress.finished_at = datetime.now().isoformat()
        if _progress.status == "running":
            _progress.status = "done"


def _do_export(drive_letter: str) -> dict:
    global _progress
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables
    from sqlalchemy import text

    drive = Path(f"{drive_letter}/")
    if not drive.exists():
        _progress.status = "error"
        _progress.message = f"Drive {drive_letter} not found"
        return get_progress()

    # Check free space
    usage = shutil.disk_usage(str(drive))
    _progress.message = f"Drive {drive_letter} — {usage.free / (1024**3):.1f} GB free"

    # Flush WAL before reading
    _progress.phase = "reading"
    _progress.message = "Reading Rekordbox library..."
    db = Rekordbox6Database()
    db.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    db.session.commit()

    # Find FF playlists
    prefix = os.getenv("PLAYLIST_PREFIX", "FF")
    ff_playlists = []

    for pl in db.session.query(tables.DjmdPlaylist).all():
        name = str(pl.Name or "")
        # Match playlists created by File Fetcher (display names without prefix)
        # These are playlists at root level that match known FF display names
        # Read sync_state to find which playlists we manage
        ff_playlists.append(pl)

    # Better: read sync_state.json to find which playlists are ours
    import json
    state_file = Path(__file__).parent.parent / "sync_state.json"
    managed_names = set()
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            # Get display names from the playlists we track
            from services.spotify import SpotifyService
            # Can't call Spotify here (no auth context). Instead, derive from state.
            # The playlist names in Rekordbox are the display names (prefix stripped)
            # We need to find them. Let's just look for playlists that have our imported tracks.
            for pid, pl_data in state.get("playlists", {}).items():
                tracks = pl_data.get("tracks", {})
                if tracks:
                    # Get any track filename to find the playlist name from Rekordbox
                    first_track = list(tracks.values())[0]
                    # The display_name was used when creating the playlist
                    # We stored it... let's check if it's in the state
                    pass
        except Exception:
            pass

    # Simpler approach: find all playlists that have tracks in Music Backup/Incoming
    ff_playlists = []
    music_folder = os.getenv("MUSIC_FOLDER", "D:/Music Backup/Incoming")

    for pl in db.session.query(tables.DjmdPlaylist).all():
        name = str(pl.Name or "")
        songs = db.session.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=pl.ID).all()
        if not songs:
            continue

        # Check if any track in this playlist is from our music folder
        is_ff = False
        for song in songs[:3]:  # Check first 3 tracks
            content = db.session.query(tables.DjmdContent).filter_by(ID=song.ContentID).first()
            if content:
                fp = str(getattr(content, 'FolderPath', '') or '')
                if music_folder.replace("\\", "/") in fp.replace("\\", "/"):
                    is_ff = True
                    break

        if is_ff:
            ff_playlists.append({
                "id": str(pl.ID),
                "name": name,
                "songs": songs,
            })

    _progress.playlists_total = len(ff_playlists)
    logger.info("Found %d FF playlists to export", len(ff_playlists))

    if not ff_playlists:
        _progress.status = "done"
        _progress.message = "No FF playlists found to export"
        return get_progress()

    # Create USB directory structure
    pioneer_dir = drive / "PIONEER"
    anlz_usb_dir = pioneer_dir / "USBANLZ"
    rb_dir = pioneer_dir / "rekordbox"
    contents_dir = drive / "Contents"
    anlz_usb_dir.mkdir(parents=True, exist_ok=True)
    rb_dir.mkdir(parents=True, exist_ok=True)
    contents_dir.mkdir(parents=True, exist_ok=True)

    # Export each playlist
    all_exported_tracks = []  # For XML generation

    for pl_info in ff_playlists:
        pl_name = pl_info["name"]
        songs = pl_info["songs"]

        _progress.phase = f"exporting"
        _progress.message = f"Exporting: {pl_name}"
        logger.info("Exporting playlist: %s (%d tracks)", pl_name, len(songs))

        playlist_tracks = []
        pl_contents_dir = contents_dir / pl_name
        pl_contents_dir.mkdir(parents=True, exist_ok=True)

        for song in songs:
            content = db.session.query(tables.DjmdContent).filter_by(ID=song.ContentID).first()
            if not content:
                continue

            fp = str(getattr(content, 'FolderPath', '') or '')
            title = str(getattr(content, 'Title', '') or '')
            bpm = getattr(content, 'BPM', 0) or 0
            analysed = getattr(content, 'Analysed', 0)
            anlz_path = str(getattr(content, 'AnalysisDataPath', '') or '')

            if not fp or not Path(fp).exists():
                _progress.errors.append(f"File not found: {title}")
                continue

            _progress.tracks_total += 1
            src = Path(fp)
            dst = pl_contents_dir / src.name
            usb_relative = f"Contents/{pl_name}/{src.name}"

            # Copy audio file (skip if already exists and same size)
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                _progress.tracks_skipped += 1
            else:
                _progress.message = f"Copying: {src.name}"
                try:
                    shutil.copy2(str(src), str(dst))
                    _progress.tracks_copied += 1
                except Exception as e:
                    _progress.errors.append(f"Copy failed: {src.name}: {e}")
                    continue

            # Copy ANLZ files (if track is analyzed)
            usb_anlz_dir = None
            if analysed == 105 and anlz_path:
                usb_anlz_dir = _copy_anlz_to_usb(
                    anlz_path, str(content.ID), usb_relative,
                    str(anlz_usb_dir),
                )

            # Gather track info for XML
            key_name = ""
            if getattr(content, 'KeyID', None):
                key_obj = db.session.query(tables.DjmdKey).filter_by(ID=content.KeyID).first()
                if key_obj:
                    key_name = str(key_obj.ScaleName or "")

            artist_name = ""
            if getattr(content, 'ArtistID', None):
                artist_obj = db.session.query(tables.DjmdArtist).filter_by(ID=content.ArtistID).first()
                if artist_obj:
                    artist_name = str(artist_obj.Name or "")
            if not artist_name and " - " in title:
                artist_name = title.split(" - ", 1)[0]

            playlist_tracks.append({
                "content_id": str(content.ID),
                "title": title,
                "artist": artist_name,
                "bpm": bpm / 100.0 if bpm else 0,
                "key": key_name,
                "duration_s": 0,
                "usb_path": str(dst).replace("\\", "/"),
                "usb_relative": usb_relative,
                "anlz_dir": usb_anlz_dir,
                "track_no": song.TrackNo,
            })

        all_exported_tracks.append({
            "name": pl_name,
            "tracks": sorted(playlist_tracks, key=lambda t: t["track_no"]),
        })
        _progress.playlists_exported += 1

    # Write Rekordbox XML
    _progress.phase = "writing"
    _progress.message = "Writing Rekordbox XML..."
    _write_rekordbox_xml(str(rb_dir), all_exported_tracks, drive_letter)

    db.session.close()
    db.engine.dispose()

    _progress.status = "done"
    _progress.message = (
        f"Export complete: {_progress.playlists_exported} playlists, "
        f"{_progress.tracks_copied} copied, {_progress.tracks_skipped} skipped"
    )
    logger.info("=== USB export complete: %s ===", _progress.message)
    return get_progress()


def _copy_anlz_to_usb(anlz_data_path: str, content_id: str, usb_relative: str, usb_anlz_root: str) -> str | None:
    """Copy ANLZ files from local machine to USB, rewriting PPTH paths."""
    try:
        # Local ANLZ path
        share_root = os.path.join(os.environ.get("APPDATA", ""), "Pioneer", "rekordbox", "share")
        local_anlz_dir = os.path.join(share_root, anlz_data_path.lstrip("/").rsplit("/", 1)[0])

        if not os.path.exists(local_anlz_dir):
            return None

        # Create USB ANLZ directory
        track_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, content_id))
        usb_dir = os.path.join(usb_anlz_root, track_uuid[:3], track_uuid)
        os.makedirs(usb_dir, exist_ok=True)

        # Copy and rewrite each ANLZ file
        for fname in ["ANLZ0000.DAT", "ANLZ0000.EXT"]:
            src = os.path.join(local_anlz_dir, fname)
            dst = os.path.join(usb_dir, fname)
            if os.path.exists(src):
                data = _rewrite_ppth(src, usb_relative)
                with open(dst, "wb") as f:
                    f.write(data)

        return usb_dir

    except Exception as e:
        logger.warning("Failed to copy ANLZ for %s: %s", content_id, e)
        return None


def _rewrite_ppth(anlz_path: str, new_path: str) -> bytes:
    """Read an ANLZ file and rewrite the PPTH tag with a new path."""
    with open(anlz_path, "rb") as f:
        data = bytearray(f.read())

    # Find PPTH tag
    ppth_pos = data.find(b"PPTH")
    if ppth_pos < 0:
        return bytes(data)

    # PPTH structure: "PPTH" (4) + header_len (4) + tag_len (4) + path_len (4) + path_bytes
    header_len = struct.unpack_from(">I", data, ppth_pos + 4)[0]
    old_tag_len = struct.unpack_from(">I", data, ppth_pos + 8)[0]

    # Build new PPTH
    normalized = new_path.replace("\\", "/")
    path_bytes = normalized.encode("utf-16-be")
    new_path_len = len(path_bytes)
    new_tag_len = 16 + new_path_len

    new_ppth = struct.pack(">4sII", b"PPTH", 16, new_tag_len)
    new_ppth += struct.pack(">I", new_path_len)
    new_ppth += path_bytes

    # Replace in data
    old_ppth_end = ppth_pos + old_tag_len
    result = bytes(data[:ppth_pos]) + new_ppth + bytes(data[old_ppth_end:])

    # Update file header total length
    new_total = len(result)
    result = result[:8] + struct.pack(">I", new_total) + result[12:]

    return result


def _write_rekordbox_xml(rb_dir: str, playlists: list[dict], drive_letter: str):
    """Write Rekordbox-compatible XML for CDJ import."""
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    product = ET.SubElement(root, "PRODUCT", Name="rekordbox", Version="6.8.5", Company="AlphaTheta")
    collection = ET.SubElement(root, "COLLECTION")

    track_id_map = {}  # content_id -> xml_track_id
    next_id = 1

    # Add all tracks to collection
    for pl in playlists:
        for track in pl["tracks"]:
            cid = track["content_id"]
            if cid in track_id_map:
                continue

            tid = str(next_id)
            next_id += 1
            track_id_map[cid] = tid

            usb_path = track["usb_path"]
            file_uri = "file://localhost/" + usb_path.replace(" ", "%20").replace(drive_letter + "/", drive_letter + "/")

            attrs = {
                "TrackID": tid,
                "Name": track["title"],
                "Artist": track["artist"],
                "TotalTime": str(int(track.get("duration_s", 0))),
                "AverageBpm": f"{track['bpm']:.2f}" if track["bpm"] else "0.00",
                "Tonality": track.get("key", ""),
                "BitRate": "320",
                "SampleRate": "44100",
                "Location": file_uri,
            }
            ET.SubElement(collection, "TRACK", **{k: v for k, v in attrs.items() if v})

    collection.set("Entries", str(len(track_id_map)))

    # Add playlists
    playlists_node = ET.SubElement(root, "PLAYLISTS")
    root_node = ET.SubElement(playlists_node, "NODE", Type="0", Name="ROOT", Count=str(len(playlists)))

    for pl in playlists:
        pl_node = ET.SubElement(root_node, "NODE", Name=pl["name"], Type="1",
                                KeyType="0", Entries=str(len(pl["tracks"])))
        for track in pl["tracks"]:
            cid = track["content_id"]
            tid = track_id_map.get(cid, "0")
            ET.SubElement(pl_node, "TRACK", Key=tid)

    # Write XML
    tree = ET.ElementTree(root)
    xml_path = os.path.join(rb_dir, "rekordbox.xml")
    tree.write(xml_path, encoding="unicode", xml_declaration=True)
    logger.info("Wrote Rekordbox XML: %s (%d tracks, %d playlists)",
                xml_path, len(track_id_map), len(playlists))
