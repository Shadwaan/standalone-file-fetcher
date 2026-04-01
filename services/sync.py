"""
Sync orchestrator.

Coordinates the full pipeline: discover Spotify playlists → download new tracks
→ analyze → import to Rekordbox + Traktor → create/sync playlists.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from models.track import TrackInfo

logger = logging.getLogger(__name__)

# State file for tracking what's been synced
STATE_FILE = Path(__file__).parent.parent / "sync_state.json"


@dataclass
class SyncProgress:
    """Tracks progress of a sync operation."""
    status: str = "idle"  # idle, running, done, error
    phase: str = ""
    message: str = ""
    playlists_found: int = 0
    tracks_total: int = 0
    tracks_downloaded: int = 0
    tracks_analyzed: int = 0
    tracks_imported: int = 0
    tracks_skipped: int = 0
    tracks_failed: int = 0
    tracks_removed: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    playlist_details: list[dict] = field(default_factory=list)


class SyncOrchestrator:
    """Orchestrates the full Spotify → Rekordbox/Traktor sync pipeline."""

    def __init__(self):
        self.progress = SyncProgress()
        self._state = self._load_state()
        self._running = False

    def _load_state(self) -> dict:
        """Load sync state from disk."""
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"playlists": {}, "last_sync": None}

    def _save_state(self):
        """Save sync state to disk."""
        try:
            STATE_FILE.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except OSError as e:
            logger.error("Failed to save state: %s", e)

    def get_progress(self) -> dict:
        """Get current sync progress as a dict."""
        return {
            "status": self.progress.status,
            "phase": self.progress.phase,
            "message": self.progress.message,
            "playlists_found": self.progress.playlists_found,
            "tracks_total": self.progress.tracks_total,
            "tracks_downloaded": self.progress.tracks_downloaded,
            "tracks_analyzed": self.progress.tracks_analyzed,
            "tracks_imported": self.progress.tracks_imported,
            "tracks_skipped": self.progress.tracks_skipped,
            "tracks_failed": self.progress.tracks_failed,
            "tracks_removed": self.progress.tracks_removed,
            "errors": self.progress.errors,
            "started_at": self.progress.started_at,
            "finished_at": self.progress.finished_at,
            "playlist_details": self.progress.playlist_details,
            "last_sync": self._state.get("last_sync"),
        }

    def run_sync(self) -> dict:
        """Run a full sync cycle."""
        if self._running:
            return {"error": "Sync already in progress"}

        self._running = True
        self.progress = SyncProgress(
            status="running",
            started_at=datetime.now().isoformat(),
        )

        try:
            return self._do_sync()
        except Exception as e:
            logger.error("Sync failed: %s", e)
            self.progress.status = "error"
            self.progress.message = str(e)
            self.progress.errors.append(str(e))
            return self.get_progress()
        finally:
            self.progress.finished_at = datetime.now().isoformat()
            if self.progress.status == "running":
                self.progress.status = "done"
            self._state["last_sync"] = self.progress.finished_at
            self._save_state()
            self._running = False

    def _build_file_index(self, search_dirs: list[str]) -> dict[str, str]:
        """Build a lowercase filename → full path index across all music directories."""
        index = {}
        audio_exts = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".aiff"}
        for root_dir in search_dirs:
            root_path = Path(root_dir)
            if not root_path.exists():
                continue
            for dirpath, _, filenames in os.walk(root_dir):
                for fname in filenames:
                    if Path(fname).suffix.lower() in audio_exts:
                        index[fname.lower()] = os.path.join(dirpath, fname).replace("\\", "/")
        logger.info("File index: %d audio files across %d directories", len(index), len(search_dirs))
        return index

    def _is_rekordbox_running(self) -> bool:
        """Check if Rekordbox is running."""
        import psutil
        for proc in psutil.process_iter(['name']):
            try:
                if 'rekordbox' in proc.info['name'].lower():
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return False

    def _do_sync(self) -> dict:
        """Execute the full sync pipeline."""
        # Check if Rekordbox is running — refuse to sync to avoid DB corruption
        if self._is_rekordbox_running():
            self.progress.status = "error"
            self.progress.message = "Rekordbox is running. Please close it before syncing."
            self.progress.errors.append("Rekordbox must be closed before syncing")
            logger.error("Sync aborted: Rekordbox is running")
            return self.get_progress()

        from services.spotify import SpotifyService
        from services.downloader import download_track
        from services import rekordbox as rb
        from services import traktor as tk

        music_folder = os.getenv("MUSIC_FOLDER", "D:/Music Backup/Incoming")
        prefix = os.getenv("PLAYLIST_PREFIX", "FF")

        # Build file index across all music directories for duplicate detection
        search_dirs = [music_folder, "D:/Music Backup"]
        self.progress.phase = "indexing"
        self.progress.message = "Scanning music directories..."
        file_index = self._build_file_index(search_dirs)

        # Phase 1: Discover playlists
        self.progress.phase = "discovering"
        self.progress.message = "Connecting to Spotify..."
        logger.info("=== Sync started ===")

        spotify = SpotifyService()
        playlists = spotify.get_prefixed_playlists()
        self.progress.playlists_found = len(playlists)
        self.progress.message = f"Found {len(playlists)} playlists"

        # Track which playlists are still active
        active_playlist_names = set()

        for pl in playlists:
            pl_id = pl["id"]
            pl_name = pl["name"]
            display_name = pl["display_name"]  # Prefix stripped
            active_playlist_names.add(display_name)

            self.progress.phase = "syncing"
            self.progress.message = f"Processing playlist: {pl_name}"
            logger.info("Processing playlist: %s → '%s'", pl_name, display_name)

            # Get current tracks from Spotify
            spotify_tracks = spotify.get_playlist_tracks(pl_id, pl_name)
            pl_state = self._state["playlists"].get(pl_id, {"tracks": {}, "snapshot_id": ""})

            # Detect what's new
            known_ids = set(pl_state.get("tracks", {}).keys())
            current_ids = {t.spotify_id for t in spotify_tracks}

            new_tracks = [t for t in spotify_tracks if t.spotify_id not in known_ids]
            removed_ids = known_ids - current_ids

            playlist_detail = {
                "name": pl_name,
                "display_name": display_name,
                "total": len(spotify_tracks),
                "new": len(new_tracks),
                "removed": len(removed_ids),
            }
            self.progress.playlist_details.append(playlist_detail)
            self.progress.tracks_total += len(new_tracks)

            # Ensure playlists exist in both Rekordbox and Traktor
            rb_playlist_id = rb.find_or_create_playlist(display_name)
            tk.find_or_create_playlist(display_name)

            # Process new tracks
            for track in new_tracks:
                self.progress.message = f"Checking: {track.artist} - {track.title}"
                logger.info("New track: %s - %s", track.artist, track.title)

                # Check if track already exists in Rekordbox library (by artist+title)
                existing = rb.find_content_by_title(track.artist, track.title)
                if existing:
                    rb_content_id, existing_path = existing
                    self.progress.tracks_skipped += 1
                    logger.info("Already in Rekordbox: %s (ID=%s, path=%s)", track.title, rb_content_id, existing_path)

                    # Just add to playlists (track already in library)
                    if rb_playlist_id:
                        rb.add_track_to_playlist(rb_playlist_id, rb_content_id, track.position + 1)
                    if existing_path:
                        tk.add_track_to_playlist(display_name, existing_path, track.position)

                    # Track in state
                    if pl_id not in self._state["playlists"]:
                        self._state["playlists"][pl_id] = {"tracks": {}, "snapshot_id": ""}
                    self._state["playlists"][pl_id]["tracks"][track.spotify_id] = {
                        "filename": Path(existing_path).name if existing_path else track.filename,
                        "file_path": existing_path or "",
                        "artist": track.artist,
                        "title": track.title,
                    }
                    continue

                # Check if file already exists anywhere in music directories
                dest_path = None
                existing_file = file_index.get(track.filename.lower())
                if existing_file and Path(existing_file).exists():
                    dest_path = Path(existing_file)
                    self.progress.tracks_skipped += 1
                    logger.info("File found in library: %s", dest_path)
                elif (Path(music_folder) / display_name / track.filename).exists():
                    dest_path = Path(music_folder) / display_name / track.filename
                    self.progress.tracks_skipped += 1
                    logger.info("File found in playlist folder: %s", dest_path)
                elif (Path(music_folder) / track.filename).exists():
                    dest_path = Path(music_folder) / track.filename
                    self.progress.tracks_skipped += 1
                    logger.info("File found in music folder: %s", dest_path)

                if dest_path is None:
                    # Download into playlist subfolder
                    self.progress.message = f"Downloading: {track.artist} - {track.title}"
                    playlist_folder = Path(music_folder) / display_name
                    playlist_folder.mkdir(parents=True, exist_ok=True)

                    with tempfile.TemporaryDirectory(prefix="sff_dl_") as tmp_dir:
                        downloaded = download_track(track, tmp_dir)
                        if not downloaded:
                            self.progress.tracks_failed += 1
                            self.progress.errors.append(f"Download failed: {track.artist} - {track.title}")
                            continue

                        self.progress.tracks_downloaded += 1

                        # Move to playlist subfolder
                        import shutil
                        final_path = playlist_folder / downloaded.name
                        counter = 1
                        while final_path.exists():
                            final_path = playlist_folder / f"{downloaded.stem}_{counter}{downloaded.suffix}"
                            counter += 1
                        shutil.move(str(downloaded), str(final_path))
                        dest_path = final_path

                file_path = str(dest_path).replace("\\", "/")

                # Import to Rekordbox (unanalyzed — let Rekordbox analyze)
                self.progress.message = f"Importing to Rekordbox: {track.title}"
                rb_result = rb.import_track_unanalyzed(file_path, track)
                rb_content_id = rb_result.get("id")

                # Import to Traktor (basic entry — let Traktor analyze)
                self.progress.message = f"Importing to Traktor: {track.title}"
                tk.import_track_unanalyzed(file_path, track)

                self.progress.tracks_imported += 1

                # Add to playlists
                if rb_playlist_id and rb_content_id:
                    rb.add_track_to_playlist(rb_playlist_id, rb_content_id, track.position + 1)
                tk.add_track_to_playlist(display_name, file_path, track.position)

                # Track in state
                if pl_id not in self._state["playlists"]:
                    self._state["playlists"][pl_id] = {"tracks": {}, "snapshot_id": ""}
                self._state["playlists"][pl_id]["tracks"][track.spotify_id] = {
                    "filename": dest_path.name,
                    "file_path": file_path,
                    "artist": track.artist,
                    "title": track.title,
                }

            # Handle removals — remove from playlists only (NEVER delete tracks)
            if removed_ids:
                self.progress.message = f"Cleaning playlist: {display_name}"
                for rid in removed_ids:
                    track_info = pl_state.get("tracks", {}).get(rid, {})
                    filename = track_info.get("filename", "")
                    if filename:
                        rb.remove_track_from_playlist(display_name, filename)
                        tk.remove_track_from_playlist(display_name, filename)
                        self.progress.tracks_removed += 1
                        logger.info("Removed '%s' from playlists (track kept in library)", filename)

                    # Remove from state
                    if pl_id in self._state["playlists"]:
                        self._state["playlists"][pl_id]["tracks"].pop(rid, None)

            # Sync playlist ordering
            self.progress.message = f"Syncing order: {display_name}"
            filenames_ordered = []
            for track in spotify_tracks:
                tid = track.spotify_id
                track_state = self._state["playlists"].get(pl_id, {}).get("tracks", {}).get(tid, {})
                fn = track_state.get("filename", track.filename)
                filenames_ordered.append(fn)

            rb.sync_playlist_order(display_name, filenames_ordered)
            tk.sync_playlist_order(display_name, filenames_ordered)

            # Update state
            if pl_id not in self._state["playlists"]:
                self._state["playlists"][pl_id] = {"tracks": {}, "snapshot_id": ""}
            self._state["playlists"][pl_id]["snapshot_id"] = pl.get("snapshot_id", "")

        self.progress.status = "done"
        self.progress.phase = "complete"
        self.progress.message = (
            f"Sync complete: {self.progress.tracks_imported} imported, "
            f"{self.progress.tracks_skipped} skipped, "
            f"{self.progress.tracks_failed} failed, "
            f"{self.progress.tracks_removed} removed from playlists"
        )
        logger.info("=== Sync complete: %s ===", self.progress.message)

        # Flush WAL so Rekordbox can see our changes
        rb.flush_wal()

        # NOTE: Auto-analyze via GUI automation is DISABLED.
        # Rekordbox's Ctrl+A selects ALL tracks in Collection, causing it to
        # re-analyze already-analyzed tracks. There is no way to filter/select
        # only unanalyzed tracks via GUI automation.
        # Instead: after sync, open Rekordbox manually, go to each new FF playlist,
        # select all, right-click → Analyse Track. This only analyzes that playlist.
        if self.progress.tracks_downloaded > 0:
            try:
                from services.rekordbox_auto import _count_unanalyzed
                unanalyzed = _count_unanalyzed()
                if unanalyzed > 0:
                    self.progress.message = (
                        f"Sync complete. {unanalyzed} tracks need analysis in Rekordbox. "
                        f"Open Rekordbox → select new FF playlists → Analyse Track."
                    )
            except Exception:
                pass

        self._save_state()
        return self.get_progress()
