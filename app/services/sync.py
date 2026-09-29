"""
Sync orchestrator.

Coordinates the full pipeline: discover Spotify playlists → download new tracks
→ analyze → import to Rekordbox + Traktor → create/sync playlists.
"""

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from models.track import TrackInfo
from services import app_config
from services.platform_paths import (
    is_rekordbox_running as _platform_is_rekordbox_running,
)

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


@dataclass
class _PlaylistContext:
    """Everything the per-track pipeline needs about the playlist being synced."""
    pl_id: str
    pl_name: str
    display_name: str
    rb_playlist_id: object
    rb: object
    tk: object
    download_track: object
    music_folder: str
    file_index: dict


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
            "failed_pending": [
                {
                    "artist": f["artist"],
                    "title": f["title"],
                    "playlist": f["display_name"],
                    "attempts": f.get("attempts", 1),
                }
                for f in self._state.get("failed", {}).values()
            ],
        }

    def run_sync(self) -> dict:
        """Run a full sync cycle."""
        return self._run(self._do_sync, full_sync=True)

    def run_retry_failed(self) -> dict:
        """Re-attempt only the tracks whose download failed in earlier runs."""
        return self._run(self._do_retry_failed, full_sync=False)

    def _run(self, job, full_sync: bool) -> dict:
        if self._running:
            return {"error": "Sync already in progress"}

        self._running = True
        self.progress = SyncProgress(
            status="running",
            started_at=datetime.now().isoformat(),
        )

        try:
            return job()
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
            if full_sync:
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
        """Check if Rekordbox is running (cross-platform via platform_paths)."""
        return _platform_is_rekordbox_running()

    def stop_syncing_playlist(self, playlist_id_or_name: str) -> dict:
        """Remove a playlist from sync_state. Does NOT touch Rekordbox/Traktor.

        Accepts either the Spotify playlist ID (key in self._state['playlists'])
        or the user-facing display name. Re-prefixing the playlist with FF in
        Spotify and re-syncing will pick it up again.
        """
        playlists = self._state.get("playlists", {})
        if not playlists:
            return {"removed": False, "reason": "no playlists in sync state"}

        target_id = None
        if playlist_id_or_name in playlists:
            target_id = playlist_id_or_name
        else:
            for pid, pdata in playlists.items():
                if pdata.get("name") == playlist_id_or_name or pdata.get("display_name") == playlist_id_or_name:
                    target_id = pid
                    break

        if target_id is None:
            return {"removed": False, "reason": f"playlist not found: {playlist_id_or_name}"}

        removed = playlists.pop(target_id)
        self._save_state()
        logger.info("Stopped syncing playlist: %s (id=%s)", removed.get("name", target_id), target_id)
        return {
            "removed": True,
            "playlist_id": target_id,
            "name": removed.get("name"),
            "tracks_dropped_from_state": len(removed.get("tracks", {})),
        }

    def _refuse_if_rekordbox_running(self) -> bool:
        """Refuse to touch master.db while Rekordbox has it open (corrupts it)."""
        if not self._is_rekordbox_running():
            return False
        self.progress.status = "error"
        self.progress.message = "Rekordbox is running. Please close it before syncing."
        self.progress.errors.append("Rekordbox must be closed before syncing")
        logger.error("Sync aborted: Rekordbox is running")
        return True

    def _traktor_module(self):
        """Traktor sync is opt-in via the UI checkbox (or legacy ENABLE_TRAKTOR=1)."""
        if app_config.get_sync_to_traktor():
            from services import traktor
            logger.info("Traktor sync ENABLED")
            return traktor
        logger.info("Traktor sync disabled (toggle 'Sync to Traktor' in the UI to enable)")
        return None

    def _build_music_index(self, music_folder: str) -> dict[str, str]:
        """Index the music folder, plus its parent so legacy MP3s outside
        Incoming/ still get caught by dedup."""
        parent_dir = str(Path(music_folder).parent)
        search_dirs = [music_folder]
        if parent_dir and parent_dir != music_folder:
            search_dirs.append(parent_dir)
        self.progress.phase = "indexing"
        self.progress.message = "Scanning music directories..."
        return self._build_file_index(search_dirs)

    # ─── Failed-download queue (drives the "Retry failed" button) ─────────

    def _record_failure(self, ctx: _PlaylistContext, track: TrackInfo):
        failed = self._state.setdefault("failed", {})
        key = f"{ctx.pl_id}:{track.spotify_id}"
        failed[key] = {
            "pl_id": ctx.pl_id,
            "pl_name": ctx.pl_name,
            "display_name": ctx.display_name,
            "spotify_id": track.spotify_id,
            "artist": track.artist,
            "title": track.title,
            "attempts": failed.get(key, {}).get("attempts", 0) + 1,
            "last_attempt": datetime.now().isoformat(),
        }

    def _clear_failure(self, pl_id: str, spotify_id: str):
        self._state.get("failed", {}).pop(f"{pl_id}:{spotify_id}", None)

    def _mark_track_synced(self, ctx: _PlaylistContext, track: TrackInfo, filename: str, file_path: str):
        pl = self._state["playlists"].setdefault(ctx.pl_id, {"tracks": {}, "snapshot_id": ""})
        pl["name"] = ctx.pl_name
        pl["display_name"] = ctx.display_name
        pl["tracks"][track.spotify_id] = {
            "filename": filename,
            "file_path": file_path,
            "artist": track.artist,
            "title": track.title,
        }
        self._clear_failure(ctx.pl_id, track.spotify_id)

    def _process_track(self, ctx: _PlaylistContext, track: TrackInfo):
        """Dedup, download, import and link one track. Shared by full sync and
        retry so both go through identical Rekordbox import hygiene."""
        rb, tk = ctx.rb, ctx.tk
        self.progress.message = f"Checking: {track.artist} - {track.title}"
        logger.info("New track: %s - %s", track.artist, track.title)

        # Check if track already exists in Rekordbox library (by artist+title)
        existing = rb.find_content_by_title(track.artist, track.title)
        if existing:
            rb_content_id, existing_path = existing
            self.progress.tracks_skipped += 1
            logger.info("Already in Rekordbox: %s (ID=%s, path=%s)", track.title, rb_content_id, existing_path)

            # Just add to playlists (track already in library)
            if ctx.rb_playlist_id:
                rb.add_track_to_playlist(ctx.rb_playlist_id, rb_content_id, track.position + 1)
            if tk and existing_path:
                tk.add_track_to_playlist(ctx.display_name, existing_path, track.position)

            self._mark_track_synced(
                ctx, track,
                Path(existing_path).name if existing_path else track.filename,
                existing_path or "",
            )
            return

        # Check if file already exists anywhere in music directories
        dest_path = None
        existing_file = ctx.file_index.get(track.filename.lower())
        if existing_file and Path(existing_file).exists():
            dest_path = Path(existing_file)
            self.progress.tracks_skipped += 1
            logger.info("File found in library: %s", dest_path)
        elif (Path(ctx.music_folder) / ctx.display_name / track.filename).exists():
            dest_path = Path(ctx.music_folder) / ctx.display_name / track.filename
            self.progress.tracks_skipped += 1
            logger.info("File found in playlist folder: %s", dest_path)
        elif (Path(ctx.music_folder) / track.filename).exists():
            dest_path = Path(ctx.music_folder) / track.filename
            self.progress.tracks_skipped += 1
            logger.info("File found in music folder: %s", dest_path)

        if dest_path is None:
            # Download into playlist subfolder
            self.progress.message = f"Downloading: {track.artist} - {track.title}"
            playlist_folder = Path(ctx.music_folder) / ctx.display_name
            playlist_folder.mkdir(parents=True, exist_ok=True)

            with tempfile.TemporaryDirectory(prefix="sff_dl_") as tmp_dir:
                downloaded = ctx.download_track(track, tmp_dir)
                if not downloaded:
                    self.progress.tracks_failed += 1
                    self.progress.errors.append(f"Download failed: {track.artist} - {track.title}")
                    self._record_failure(ctx, track)
                    return

                self.progress.tracks_downloaded += 1

                # Move to playlist subfolder
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

        # Import to Traktor (basic entry — let Traktor analyze) — opt-in
        if tk:
            self.progress.message = f"Importing to Traktor: {track.title}"
            tk.import_track_unanalyzed(file_path, track)

        self.progress.tracks_imported += 1

        # Add to playlists
        if ctx.rb_playlist_id and rb_content_id:
            rb.add_track_to_playlist(ctx.rb_playlist_id, rb_content_id, track.position + 1)
        if tk:
            tk.add_track_to_playlist(ctx.display_name, file_path, track.position)

        self._mark_track_synced(ctx, track, dest_path.name, file_path)

    def _sync_playlist_order(self, ctx: _PlaylistContext, spotify_tracks: list[TrackInfo]):
        self.progress.message = f"Syncing order: {ctx.display_name}"
        filenames_ordered = []
        for track in spotify_tracks:
            track_state = self._state["playlists"].get(ctx.pl_id, {}).get("tracks", {}).get(track.spotify_id, {})
            filenames_ordered.append(track_state.get("filename", track.filename))

        ctx.rb.sync_playlist_order(ctx.display_name, filenames_ordered)
        if ctx.tk:
            ctx.tk.sync_playlist_order(ctx.display_name, filenames_ordered)

    def _do_retry_failed(self) -> dict:
        """Re-run only the queued failed downloads through the normal per-track
        pipeline, without rescanning every playlist."""
        queued = list(self._state.get("failed", {}).values())
        if not queued:
            self.progress.status = "done"
            self.progress.phase = "complete"
            self.progress.message = "No failed downloads to retry"
            return self.get_progress()

        if self._refuse_if_rekordbox_running():
            return self.get_progress()

        from services.spotify import SpotifyService
        from services.downloader import download_track
        from services import rekordbox as rb

        music_folder = app_config.get_music_folder()
        tk = self._traktor_module()
        file_index = self._build_music_index(music_folder)

        self.progress.phase = "retrying"
        self.progress.tracks_total = len(queued)
        logger.info("=== Retrying %d failed downloads ===", len(queued))
        spotify = SpotifyService()

        by_playlist: dict[str, list[dict]] = {}
        for f in queued:
            by_playlist.setdefault(f["pl_id"], []).append(f)

        try:
            for pl_id, entries in by_playlist.items():
                pl_name = entries[0]["pl_name"]
                display_name = entries[0]["display_name"]
                self.progress.message = f"Processing playlist: {pl_name}"

                # Fresh Spotify data: positions may have shifted, and tracks
                # removed from the playlist since shouldn't be fetched at all.
                spotify_tracks = spotify.get_playlist_tracks(pl_id, pl_name)
                current = {t.spotify_id: t for t in spotify_tracks}

                rb_playlist_id = rb.find_or_create_playlist(display_name)
                if tk:
                    tk.find_or_create_playlist(display_name)
                ctx = _PlaylistContext(
                    pl_id, pl_name, display_name, rb_playlist_id,
                    rb, tk, download_track, music_folder, file_index,
                )

                for f in entries:
                    track = current.get(f["spotify_id"])
                    if track is None:
                        logger.info("No longer in %s, dropping from retry queue: %s - %s",
                                    pl_name, f["artist"], f["title"])
                        self._clear_failure(pl_id, f["spotify_id"])
                        continue
                    self._process_track(ctx, track)

                self._sync_playlist_order(ctx, spotify_tracks)
        finally:
            # Always flush, even if a playlist errored midway — otherwise the
            # tracks already imported stay invisible to Rekordbox.
            rb.flush_wal()

        self.progress.status = "done"
        self.progress.phase = "complete"
        self.progress.message = (
            f"Retry complete: {self.progress.tracks_imported} imported, "
            f"{self.progress.tracks_skipped} skipped, "
            f"{self.progress.tracks_failed} still failing"
        )
        logger.info("=== %s ===", self.progress.message)
        self._save_state()
        return self.get_progress()

    def _do_sync(self) -> dict:
        """Execute the full sync pipeline."""
        if app_config.get_download_source() == "soulseek":
            return self._do_sync_soulseek()

        if self._refuse_if_rekordbox_running():
            return self.get_progress()

        from services.spotify import SpotifyService
        from services.downloader import download_track
        from services import rekordbox as rb

        music_folder = app_config.get_music_folder()
        prefix = os.getenv("PLAYLIST_PREFIX", "FF")

        tk = self._traktor_module()
        file_index = self._build_music_index(music_folder)

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

            # A queued failure for a track no longer in the playlist is moot
            for key, f in list(self._state.get("failed", {}).items()):
                if f["pl_id"] == pl_id and f["spotify_id"] not in current_ids:
                    self._state["failed"].pop(key)

            playlist_detail = {
                "name": pl_name,
                "display_name": display_name,
                "total": len(spotify_tracks),
                "new": len(new_tracks),
                "removed": len(removed_ids),
            }
            self.progress.playlist_details.append(playlist_detail)
            self.progress.tracks_total += len(new_tracks)

            # Ensure playlists exist in both Rekordbox and (optionally) Traktor
            rb_playlist_id = rb.find_or_create_playlist(display_name)
            if tk:
                tk.find_or_create_playlist(display_name)

            ctx = _PlaylistContext(
                pl_id, pl_name, display_name, rb_playlist_id,
                rb, tk, download_track, music_folder, file_index,
            )
            for track in new_tracks:
                self._process_track(ctx, track)

            # Handle removals — remove from playlists only (NEVER delete tracks)
            if removed_ids:
                self.progress.message = f"Cleaning playlist: {display_name}"
                for rid in removed_ids:
                    track_info = pl_state.get("tracks", {}).get(rid, {})
                    filename = track_info.get("filename", "")
                    if filename:
                        rb.remove_track_from_playlist(display_name, filename)
                        if tk:
                            tk.remove_track_from_playlist(display_name, filename)
                        self.progress.tracks_removed += 1
                        logger.info("Removed '%s' from playlists (track kept in library)", filename)

                    # Remove from state
                    if pl_id in self._state["playlists"]:
                        self._state["playlists"][pl_id]["tracks"].pop(rid, None)

            self._sync_playlist_order(ctx, spotify_tracks)

            # Update state
            if pl_id not in self._state["playlists"]:
                self._state["playlists"][pl_id] = {"tracks": {}, "snapshot_id": "", "name": pl_name, "display_name": display_name}
            else:
                self._state["playlists"][pl_id]["name"] = pl_name
                self._state["playlists"][pl_id]["display_name"] = display_name
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

        # After sync, count unanalyzed tracks so the UI can prompt the user
        # to open Rekordbox and analyze them. (sff does NOT trigger analysis;
        # Rekordbox auto-analyzes new tracks on next launch with the import
        # hygiene we now do — proper UUID, ArtistID, AlbumID, SR/BR.)
        if self.progress.tracks_downloaded > 0:
            try:
                from pyrekordbox import Rekordbox6Database
                from pyrekordbox.db6 import tables
                _db = Rekordbox6Database()
                unanalyzed = _db.session.query(tables.DjmdContent).filter_by(Analysed=0).count()
                _db.session.close()
                _db.engine.dispose()
                if unanalyzed > 0:
                    self.progress.message = (
                        f"Sync complete. {unanalyzed} tracks need analysis in Rekordbox. "
                        f"Open Rekordbox → select new FF playlists → Analyse Track."
                    )
            except Exception:
                pass

        self._save_state()
        return self.get_progress()

    # ─── Soulseek / FLAC pipeline ───────────────────────────────────────────
    # Distinct from the yt-dlp path above: Soulseek downloads are inherently
    # unpredictable in timing (peer-dependent), so instead of a per-track
    # blocking download call, this searches+queues every new track up front,
    # then polls (self-healing stalled/dead sources) until everything is
    # resolved, and ONLY THEN does a single batch Rekordbox import. Touching
    # Rekordbox exactly once per run -- never mid-resolve -- is what keeps
    # playlist ordering from ever going stale/colliding between runs.

    def _wait_until_rekordbox_closed(self, reason: str):
        """Block until Rekordbox is closed. Writing to master.db while Rekordbox
        has it open corrupts it, and a Soulseek sync runs long enough that "it
        was closed when we started" says nothing about now -- so this is called
        right before anything touches the database, not just once at the start."""
        announced = False
        while self._is_rekordbox_running():
            if not announced:
                logger.warning("Rekordbox is open -- pausing until it's closed (%s)", reason)
                announced = True
            self.progress.phase = "waiting_rekordbox"
            self.progress.message = f"Waiting for Rekordbox to close ({reason})..."
            time.sleep(5)

    def _do_sync_soulseek(self) -> dict:
        if self._refuse_if_rekordbox_running():
            return self.get_progress()

        from services import soulseek
        from services.spotify import SpotifyService
        from services import rekordbox as rb

        status = soulseek.get_status()
        if not status["running"]:
            self.progress.phase = "starting_nicotine"
            self.progress.message = "Nicotine+ isn't running -- starting it..."
            launch_result = soulseek.launch()
            if not launch_result["api_reachable"]:
                self.progress.status = "error"
                self.progress.message = launch_result["error"] or (
                    "Nicotine+ needs to be running for Soulseek downloads. "
                    "Please start it and enable the 'API Nicotine Plus' plugin."
                )
                self.progress.errors.append(self.progress.message)
                return self.get_progress()
        elif not status["api_reachable"]:
            self.progress.status = "error"
            self.progress.message = (
                "Nicotine+ is running but its API plugin isn't reachable. "
                "Enable 'API Nicotine Plus' in Preferences -> Plugins."
            )
            self.progress.errors.append(self.progress.message)
            return self.get_progress()

        music_folder = app_config.get_music_folder()
        nicotine_download_dir = os.getenv("NICOTINE_DOWNLOAD_DIR", r"D:\Music\Nicotine")

        self.progress.phase = "discovering"
        self.progress.message = "Connecting to Spotify..."
        logger.info("=== Soulseek/FLAC sync started ===")

        spotify = SpotifyService()
        playlists = spotify.get_prefixed_playlists()
        self.progress.playlists_found = len(playlists)

        try:
            for pl in playlists:
                pl_id = pl["id"]
                pl_name = pl["name"]
                base_display_name = pl["display_name"]
                display_name = f"{base_display_name} FLAC"

                self.progress.phase = "syncing"
                self.progress.message = f"Processing playlist: {pl_name} (FLAC)"
                logger.info("Processing playlist: %s -> '%s'", pl_name, display_name)

                spotify_tracks = spotify.get_playlist_tracks(pl_id, pl_name)
                for t in spotify_tracks:
                    t.file_extension = "flac"
                    t.playlist_name = display_name

                pl_state = self._state["playlists"].setdefault(
                    pl_id, {"tracks": {}, "snapshot_id": "", "name": pl_name, "display_name": base_display_name},
                )
                flac_state = pl_state.setdefault(
                    "flac_variant", {"tracks": {}, "rb_playlist_id": None, "display_name": display_name, "created_at": None},
                )
                flac_state["display_name"] = display_name

                # Up to the import step, Rekordbox is only READ (what's already
                # done) -- nothing is written until every download has resolved.
                # Reads still wait if Rekordbox is open: a failed read looks like
                # "nothing in the library yet" and would re-download everything.
                self._wait_until_rekordbox_closed("checking what's already in your library")
                rb_playlist_id = flac_state.get("rb_playlist_id") or rb.find_playlist_id(display_name)

                # Two independent checks for "already done", not just one: our own
                # state file AND Rekordbox's actual database. State can be lost,
                # never written (e.g. a track added outside the normal Sync flow),
                # or otherwise go stale -- without this second check, that gets
                # every one of its tracks silently re-downloaded and duplicated.
                known_ids = set(flac_state["tracks"].keys())
                rb_existing_titles = rb.get_playlist_track_titles(rb_playlist_id) if rb_playlist_id else set()
                current_ids = {t.spotify_id for t in spotify_tracks}
                new_tracks = [
                    t for t in spotify_tracks
                    if t.spotify_id not in known_ids and t.title not in rb_existing_titles
                ]
                # A track Rekordbox already has but our state didn't know about --
                # backfill the state instead of silently doing nothing, so future
                # runs (and the UI's FLAC-playlist badge) see it correctly too.
                for t in spotify_tracks:
                    if t.spotify_id in known_ids or t.title not in rb_existing_titles:
                        continue
                    found = rb.find_content_by_title(t.artist, t.title)
                    file_path = found[1] if found else ""
                    flac_state["tracks"][t.spotify_id] = {
                        "filename": Path(file_path).name if file_path else t.filename,
                        "file_path": file_path, "artist": t.artist, "title": t.title,
                    }
                removed_ids = known_ids - current_ids

                self.progress.playlist_details.append({
                    "name": pl_name, "display_name": display_name,
                    "total": len(spotify_tracks), "new": len(new_tracks), "removed": len(removed_ids),
                })
                self.progress.tracks_total += len(new_tracks)

                states = {}
                if new_tracks:
                    def _progress_cb(msg):
                        self.progress.message = msg

                    self.progress.phase = "searching"
                    states = soulseek.search_and_queue_all(new_tracks, on_progress=_progress_cb)

                    self.progress.phase = "downloading"
                    soulseek.resolve_all(states, on_progress=_progress_cb)

                if new_tracks or removed_ids:
                    # Every download has resolved -- this is the first moment the
                    # run writes to Rekordbox. The sync may have been going for
                    # hours, so "was it closed when we started?" proves nothing;
                    # wait here (don't write into an open database) if it's open now.
                    self._wait_until_rekordbox_closed("tracks are ready to import")
                    self.progress.phase = "importing"
                    rb_playlist_id = rb.find_or_create_playlist(display_name)
                    flac_state["rb_playlist_id"] = rb_playlist_id
                    if not flac_state.get("created_at"):
                        flac_state["created_at"] = datetime.now().isoformat()

                if new_tracks:
                    playlist_folder = Path(music_folder) / display_name
                    playlist_folder.mkdir(parents=True, exist_ok=True)
                    originals_dir = playlist_folder / "_originals"

                    for track in new_tracks:
                        st = states[track.spotify_id]
                        if not st.downloaded:
                            self.progress.tracks_failed += 1
                            self.progress.errors.append(f"Soulseek: no source found for {track.artist} - {track.title}")
                            continue

                        src = soulseek.download_path_for(st, nicotine_download_dir)
                        if not src:
                            self.progress.tracks_failed += 1
                            self.progress.errors.append(f"Soulseek: download finished but file not found for {track.artist} - {track.title}")
                            continue

                        dest = playlist_folder / src.name
                        counter = 1
                        while dest.exists() and dest != src:
                            dest = playlist_folder / f"{src.stem}_{counter}{src.suffix}"
                            counter += 1
                        shutil.move(str(src), str(dest))
                        dest = soulseek.ensure_16bit_flac(dest, originals_dir)
                        self.progress.tracks_downloaded += 1

                        auth = soulseek.check_authenticity(dest)
                        if auth["suspect"]:
                            msg = f"Suspect FLAC (likely transcoded from lossy source): {track.artist} - {track.title} -- {auth['reason']}"
                            logger.warning(msg)
                            self.progress.errors.append(msg)

                        file_path = str(dest).replace("\\", "/")
                        rb_result = rb.import_track_unanalyzed(file_path, track)
                        content_id = rb_result.get("id")
                        if content_id:
                            rb.add_track_to_playlist(rb_playlist_id, content_id, track.position + 1)
                            self.progress.tracks_imported += 1
                            flac_state["tracks"][track.spotify_id] = {
                                "filename": dest.name, "file_path": file_path,
                                "artist": track.artist, "title": track.title,
                            }

                # Removals — remove from the FLAC playlist only, never delete files
                for rid in removed_ids:
                    info = flac_state["tracks"].get(rid, {})
                    filename = info.get("filename", "")
                    if filename:
                        rb.remove_track_from_playlist(display_name, filename)
                        self.progress.tracks_removed += 1
                    flac_state["tracks"].pop(rid, None)

                # Reorder every run, from scratch, against true Spotify order --
                # this is what keeps a late-arriving track from desyncing everything.
                if new_tracks or removed_ids:
                    ordered_titles = [t.title for t in spotify_tracks]
                    rb.reorder_playlist_by_titles(rb_playlist_id, ordered_titles)
                    # Flush per playlist, right after its writes, rather than once
                    # at the very end of a possibly hours-long run.
                    rb.flush_wal()

                if pl_id not in self._state["playlists"]:
                    self._state["playlists"][pl_id] = pl_state
                self._state["playlists"][pl_id]["snapshot_id"] = pl.get("snapshot_id", "")
                # Persist after every playlist so a mid-run interruption can't lose
                # what's already been imported.
                self._save_state()

            self.progress.status = "done"
            self.progress.phase = "complete"
            self.progress.message = (
                f"FLAC sync complete: {self.progress.tracks_imported} imported, "
                f"{self.progress.tracks_failed} failed, {self.progress.tracks_removed} removed from playlists"
            )
            logger.info("=== %s ===", self.progress.message)
        finally:
            self._save_state()

        return self.get_progress()
