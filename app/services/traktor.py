"""
Traktor NML import service.

Creates ENTRY elements in collection.nml with BPM, key (Open Key),
beat grid anchor, and manages playlists. Uses ATOMIC writes.
"""

import logging
import os
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from models.track import AnalysisResult, TrackInfo
from services.platform_paths import DEFAULT_TRAKTOR_NML

logger = logging.getLogger(__name__)

NML_PATH = os.getenv("TRAKTOR_NML_PATH", DEFAULT_TRAKTOR_NML)

# Open Key -> Traktor MUSICAL_KEY value (0-23)
# Major: 1d=C(0), 2d=G(7), 3d=D(2), 4d=A(9), 5d=E(4), 6d=B(11), 7d=F#(6), 8d=Db(1), 9d=Ab(8), 10d=Eb(3), 11d=Bb(10), 12d=F(5)
# Minor: 1m=Am(21), 2m=Em(16), 3m=Bm(23), 4m=F#m(18), 5m=Dbm(13), 6m=Abm(20), 7m=Ebm(15), 8m=Bbm(22), 9m=Fm(17), 10m=Cm(12), 11m=Gm(19), 12m=Dm(14)
MAJOR_MAP = {1: 0, 2: 7, 3: 2, 4: 9, 5: 4, 6: 11, 7: 6, 8: 1, 9: 8, 10: 3, 11: 10, 12: 5}
MINOR_MAP = {1: 21, 2: 16, 3: 23, 4: 18, 5: 13, 6: 20, 7: 15, 8: 22, 9: 17, 10: 12, 11: 19, 12: 14}

# Camelot -> Open Key
CAMELOT_TO_OPEN = {
    "1A": "1m", "2A": "2m", "3A": "3m", "4A": "4m", "5A": "5m", "6A": "6m",
    "7A": "7m", "8A": "8m", "9A": "9m", "10A": "10m", "11A": "11m", "12A": "12m",
    "1B": "1d", "2B": "2d", "3B": "3d", "4B": "4d", "5B": "5d", "6B": "6d",
    "7B": "7d", "8B": "8d", "9B": "9d", "10B": "10d", "11B": "11d", "12B": "12d",
}


def _safe_write_nml(tree: ET.ElementTree, nml_path: str):
    """Write NML atomically: write to temp file, verify, then replace original."""
    dir_path = os.path.dirname(nml_path)
    fd, tmp_path = tempfile.mkstemp(suffix=".nml.tmp", dir=dir_path)
    try:
        os.close(fd)
        tree.write(tmp_path, encoding="unicode", xml_declaration=True)

        # Verify the written file parses correctly
        ET.parse(tmp_path)

        # Backup current file
        backup_path = nml_path + ".bak"
        if os.path.exists(nml_path):
            shutil.copy2(nml_path, backup_path)

        # Atomic replace
        shutil.move(tmp_path, nml_path)
        logger.debug("NML written safely: %s", nml_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise RuntimeError(f"Safe NML write failed: {e}") from e


def _traktor_path_key(file_path: str) -> str:
    """Convert Windows path to Traktor PRIMARYKEY format."""
    p = file_path.replace("\\", "/")
    parts = p.split("/")
    volume = parts[0]
    rest = parts[1:]
    return volume + "/:" + "/:".join(rest)


def _find_entry(root: ET.Element, file_path: str) -> ET.Element | None:
    """Find a track entry in NML by file path."""
    target_key = _traktor_path_key(file_path)
    for entry in root.iter("ENTRY"):
        loc = entry.find("LOCATION")
        if loc is not None:
            volume = loc.get("VOLUME", "")
            dir_path = loc.get("DIR", "")
            filename = loc.get("FILE", "")
            key = volume + dir_path + filename
            if key == target_key:
                return entry
    return None


def _open_key_to_value(open_key: str) -> int | None:
    """Convert Open Key (e.g. '5m', '8d') to Traktor MUSICAL_KEY value (0-23)."""
    if not open_key:
        return None
    try:
        num = int(open_key[:-1])
        mode = open_key[-1]
        if mode == "d":
            return MAJOR_MAP.get(num)
        elif mode == "m":
            return MINOR_MAP.get(num)
    except (ValueError, IndexError):
        pass
    return None


def _camelot_to_open_key(camelot: str) -> str:
    """Convert Camelot notation to Open Key."""
    return CAMELOT_TO_OPEN.get(camelot.upper(), "")


# ─── Track Import ────────────────────────────────────────────────────────────

def import_track_unanalyzed(file_path: str, track: TrackInfo) -> dict:
    """Import a track into Traktor NML without analysis. Traktor will analyze on load."""
    nml_path = NML_PATH
    if not os.path.exists(nml_path):
        return {"status": "error", "error": f"NML not found: {nml_path}"}

    try:
        tree = ET.parse(nml_path)
        root = tree.getroot()

        # Check if already exists
        existing = _find_entry(root, file_path)
        if existing is not None:
            return {"status": "exists"}

        # Parse path
        p = file_path.replace("\\", "/")
        parts = p.split("/")
        volume = parts[0]
        filename = parts[-1]
        dir_parts = parts[1:-1]
        traktor_dir = "/:" + "/:".join(dir_parts) + "/:"

        # Parse artist/title from track
        collection = root.find("COLLECTION")
        if collection is None:
            return {"status": "error", "error": "COLLECTION not found in NML"}

        entry = ET.SubElement(collection, "ENTRY")
        entry.set("TITLE", track.title)
        if track.artist:
            entry.set("ARTIST", track.artist)
        entry.set("MODIFIED_DATE", "")
        entry.set("MODIFIED_TIME", "0")

        loc = ET.SubElement(entry, "LOCATION")
        loc.set("DIR", traktor_dir)
        loc.set("FILE", filename)
        loc.set("VOLUME", volume)
        loc.set("VOLUMEID", volume)

        info = ET.SubElement(entry, "INFO")
        if file_path.lower().endswith(".mp3"):
            info.set("BITRATE", "320000")

        entries_count = len(list(collection.findall("ENTRY")))
        collection.set("ENTRIES", str(entries_count))

        _safe_write_nml(tree, nml_path)
        logger.info("Imported to Traktor (unanalyzed): %s", filename)
        return {"status": "imported"}

    except Exception as e:
        logger.error("Traktor import failed for %s: %s", file_path, e)
        return {"status": "error", "error": str(e)}

def import_track(file_path: str, track: TrackInfo, analysis: AnalysisResult) -> dict:
    """Import a new track into Traktor collection.nml."""
    nml_path = NML_PATH
    if not os.path.exists(nml_path):
        return {"status": "error", "error": f"NML not found: {nml_path}"}

    try:
        tree = ET.parse(nml_path)
        root = tree.getroot()

        # Check if already exists
        existing = _find_entry(root, file_path)
        if existing is not None:
            return _update_existing(tree, existing, analysis, nml_path)

        # Parse file path into Traktor LOCATION format
        p = file_path.replace("\\", "/")
        parts = p.split("/")
        volume = parts[0]
        filename = parts[-1]
        dir_parts = parts[1:-1]
        traktor_dir = "/:" + "/:".join(dir_parts) + "/:"

        # Find COLLECTION element
        collection = root.find("COLLECTION")
        if collection is None:
            return {"status": "error", "error": "COLLECTION not found in NML"}

        # Create ENTRY
        entry = ET.SubElement(collection, "ENTRY")
        entry.set("TITLE", track.title)
        if track.artist:
            entry.set("ARTIST", track.artist)
        entry.set("MODIFIED_DATE", "")
        entry.set("MODIFIED_TIME", "0")

        # LOCATION
        loc = ET.SubElement(entry, "LOCATION")
        loc.set("DIR", traktor_dir)
        loc.set("FILE", filename)
        loc.set("VOLUME", volume)
        loc.set("VOLUMEID", volume)

        # INFO
        info = ET.SubElement(entry, "INFO")
        if analysis.duration_s:
            info.set("PLAYTIME", str(int(analysis.duration_s)))
        if file_path.lower().endswith(".mp3"):
            info.set("BITRATE", "320000")
        if analysis.key_camelot:
            open_key = _camelot_to_open_key(analysis.key_camelot)
            info.set("KEY", open_key)

        # TEMPO
        if analysis.bpm:
            tempo = ET.SubElement(entry, "TEMPO")
            tempo.set("BPM", f"{analysis.bpm:.6f}")
            tempo.set("BPM_QUALITY", "100")

        # MUSICAL_KEY
        if analysis.key_camelot:
            open_key = _camelot_to_open_key(analysis.key_camelot)
            key_val = _open_key_to_value(open_key)
            if key_val is not None:
                mkey = ET.SubElement(entry, "MUSICAL_KEY")
                mkey.set("VALUE", str(key_val))

        # Grid anchor CUE_V2
        if analysis.beat_grid:
            grid_anchor_ms = analysis.beat_grid[0] * 1000
            grid_cue = ET.SubElement(entry, "CUE_V2")
            grid_cue.set("NAME", "AutoGrid")
            grid_cue.set("DISPL_ORDER", "0")
            grid_cue.set("TYPE", "4")
            grid_cue.set("START", f"{grid_anchor_ms:.1f}")
            grid_cue.set("LEN", "0")
            grid_cue.set("REPEATS", "-1")
            grid_cue.set("HOTCUE", "-1")

        # Update COLLECTION entry count
        entries_count = len(list(collection.findall("ENTRY")))
        collection.set("ENTRIES", str(entries_count))

        _safe_write_nml(tree, nml_path)
        logger.info("Imported to Traktor: %s (BPM=%.1f, Key=%s)",
                     filename, analysis.bpm, analysis.key_camelot)
        return {"status": "imported"}

    except Exception as e:
        logger.error("Traktor import failed for %s: %s", file_path, e)
        return {"status": "error", "error": str(e)}


def _update_existing(tree: ET.ElementTree, entry: ET.Element, analysis: AnalysisResult, nml_path: str) -> dict:
    """Update BPM, key, and grid anchor on an existing Traktor entry."""
    changes = []

    if analysis.bpm:
        tempo = entry.find("TEMPO")
        if tempo is None:
            tempo = ET.SubElement(entry, "TEMPO")
        tempo.set("BPM", f"{analysis.bpm:.6f}")
        tempo.set("BPM_QUALITY", "100")
        changes.append(f"BPM={analysis.bpm:.2f}")

    if analysis.key_camelot:
        open_key = _camelot_to_open_key(analysis.key_camelot)
        info = entry.find("INFO")
        if info is None:
            info = ET.SubElement(entry, "INFO")
        info.set("KEY", open_key)

        musical_key = entry.find("MUSICAL_KEY")
        if musical_key is None:
            musical_key = ET.SubElement(entry, "MUSICAL_KEY")
        key_val = _open_key_to_value(open_key)
        if key_val is not None:
            musical_key.set("VALUE", str(key_val))
        changes.append(f"Key={analysis.key_camelot}")

    if analysis.beat_grid:
        # Remove existing grid markers
        for cue in list(entry.findall("CUE_V2")):
            if cue.get("TYPE") == "4":
                entry.remove(cue)
        grid_anchor_ms = analysis.beat_grid[0] * 1000
        grid_cue = ET.SubElement(entry, "CUE_V2")
        grid_cue.set("NAME", "AutoGrid")
        grid_cue.set("DISPL_ORDER", "0")
        grid_cue.set("TYPE", "4")
        grid_cue.set("START", f"{grid_anchor_ms:.1f}")
        grid_cue.set("LEN", "0")
        grid_cue.set("REPEATS", "-1")
        grid_cue.set("HOTCUE", "-1")
        changes.append(f"Grid={grid_anchor_ms:.1f}ms")

    _safe_write_nml(tree, nml_path)
    logger.info("Updated Traktor entry: %s", ", ".join(changes))
    return {"status": "updated", "changes": changes}


# ─── Traktor Playlist Management ────────────────────────────────────────────

def find_or_create_playlist(playlist_name: str) -> bool:
    """Find or create a Traktor playlist in collection.nml."""
    nml_path = NML_PATH
    if not os.path.exists(nml_path):
        return False

    try:
        tree = ET.parse(nml_path)
        root = tree.getroot()

        # Find PLAYLISTS section
        playlists_node = root.find("PLAYLISTS")
        if playlists_node is None:
            return False

        # Find the root NODE
        root_node = playlists_node.find("NODE")
        if root_node is None:
            return False

        # Check if playlist already exists
        for node in root_node.iter("NODE"):
            if node.get("TYPE") == "PLAYLIST" and node.get("NAME") == playlist_name:
                return True

        # Playlists must live inside the $ROOT folder's SUBNODES element —
        # Traktor ignores (and deletes on save) NODEs placed directly under
        # the root NODE — and SUBNODES.COUNT must match its child count.
        subnodes_elem = root_node.find("SUBNODES")
        if subnodes_elem is None:
            subnodes_elem = ET.SubElement(root_node, "SUBNODES")

        new_node = ET.Element("NODE")
        new_node.set("TYPE", "PLAYLIST")
        new_node.set("NAME", playlist_name)

        playlist_elem = ET.SubElement(new_node, "PLAYLIST")
        playlist_elem.set("ENTRIES", "0")
        playlist_elem.set("TYPE", "LIST")
        playlist_elem.set("UUID", uuid.uuid4().hex)

        # Insert at beginning of SUBNODES and fix its COUNT
        subnodes_elem.insert(0, new_node)
        subnodes_elem.set("COUNT", str(len(subnodes_elem.findall("NODE"))))

        _safe_write_nml(tree, nml_path)
        logger.info("Created Traktor playlist: %s (at top)", playlist_name)
        return True

    except Exception as e:
        logger.error("Failed to create Traktor playlist '%s': %s", playlist_name, e)
        return False


def add_track_to_playlist(playlist_name: str, file_path: str, position: int) -> bool:
    """Add a track to a Traktor playlist."""
    nml_path = NML_PATH
    if not os.path.exists(nml_path):
        return False

    try:
        tree = ET.parse(nml_path)
        root = tree.getroot()

        # Find the playlist
        target_pl = None
        for node in root.iter("NODE"):
            if node.get("TYPE") == "PLAYLIST" and node.get("NAME") == playlist_name:
                target_pl = node.find("PLAYLIST")
                break

        if target_pl is None:
            return False

        # Build primary key
        pk = _traktor_path_key(file_path)

        # Check if already in playlist
        for entry in target_pl.findall("ENTRY"):
            existing_pk = entry.find("PRIMARYKEY")
            if existing_pk is not None and existing_pk.get("KEY") == pk:
                return True  # Already present

        # Add entry
        entry = ET.SubElement(target_pl, "ENTRY")
        primary_key = ET.SubElement(entry, "PRIMARYKEY")
        primary_key.set("TYPE", "TRACK")
        primary_key.set("KEY", pk)

        # Update count
        entries_count = len(list(target_pl.findall("ENTRY")))
        target_pl.set("ENTRIES", str(entries_count))

        _safe_write_nml(tree, nml_path)
        return True

    except Exception as e:
        logger.error("Failed to add track to Traktor playlist: %s", e)
        return False


def sync_playlist_order(playlist_name: str, filenames: list[str]) -> str:
    """Reorder tracks in Traktor NML playlist to match filename order."""
    nml_path = NML_PATH
    if not os.path.exists(nml_path):
        return "NML not found"

    try:
        tree = ET.parse(nml_path)
        root = tree.getroot()

        # Find the playlist
        target_pl = None
        for node in root.iter("NODE"):
            if node.get("TYPE") == "PLAYLIST" and node.get("NAME") == playlist_name:
                target_pl = node.find("PLAYLIST")
                break

        if target_pl is None:
            return f"playlist '{playlist_name}' not found"

        entries = list(target_pl.findall("ENTRY"))
        if not entries:
            return "no entries in playlist"

        # Build filename -> entry map
        entry_map = {}
        for entry in entries:
            pk = entry.find("PRIMARYKEY")
            if pk is not None:
                key = pk.get("KEY", "")
                filename = key.split("/:")[-1] if "/:" in key else key.split("/")[-1]
                entry_map[filename] = entry

        # Remove all entries
        for entry in entries:
            target_pl.remove(entry)

        # Re-add in new order
        reordered = 0
        for filename in filenames:
            if filename in entry_map:
                target_pl.append(entry_map[filename])
                reordered += 1

        # Add remaining not in filenames at the end
        for filename, entry in entry_map.items():
            if filename not in filenames:
                target_pl.append(entry)

        target_pl.set("ENTRIES", str(len(list(target_pl.findall("ENTRY")))))

        _safe_write_nml(tree, nml_path)
        return f"reordered {reordered} tracks"

    except Exception as e:
        logger.error("Traktor playlist sync failed: %s", e)
        return f"error: {e}"


def remove_track_from_playlist(playlist_name: str, filename: str) -> bool:
    """Remove a track from a Traktor playlist (does NOT delete from library)."""
    nml_path = NML_PATH
    if not os.path.exists(nml_path):
        return False

    try:
        tree = ET.parse(nml_path)
        root = tree.getroot()

        target_pl = None
        for node in root.iter("NODE"):
            if node.get("TYPE") == "PLAYLIST" and node.get("NAME") == playlist_name:
                target_pl = node.find("PLAYLIST")
                break

        if target_pl is None:
            return False

        for entry in list(target_pl.findall("ENTRY")):
            pk = entry.find("PRIMARYKEY")
            if pk is not None:
                key = pk.get("KEY", "")
                entry_filename = key.split("/:")[-1] if "/:" in key else key.split("/")[-1]
                if entry_filename == filename:
                    target_pl.remove(entry)
                    target_pl.set("ENTRIES", str(len(list(target_pl.findall("ENTRY")))))
                    _safe_write_nml(tree, nml_path)
                    logger.info("Removed '%s' from Traktor playlist '%s'", filename, playlist_name)
                    return True

        return False

    except Exception as e:
        logger.error("Failed to remove track from Traktor playlist: %s", e)
        return False
