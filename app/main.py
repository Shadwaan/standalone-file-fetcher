"""
Standalone File Fetcher — FastAPI server.

Bridges Spotify playlists to Rekordbox and Traktor DJ libraries.
Run: python main.py
"""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Load environment
load_dotenv(Path(__file__).parent / ".env")

# Configure logging
# Also to sff.log, so a long sync that ends or dies while nobody is watching the
# console (a clean exit closes that window) can still be diagnosed afterwards.
from logging.handlers import RotatingFileHandler  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(),
              RotatingFileHandler(Path(__file__).parent / "sff.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")],
)
logger = logging.getLogger("sff")

# Create FastAPI app
app = FastAPI(title="Standalone File Fetcher", version="1.0.0")

# Serve frontend
frontend_dir = Path(__file__).parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

# Global sync orchestrator
_orchestrator = None
_sync_task = None


def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        from services.sync import SyncOrchestrator
        _orchestrator = SyncOrchestrator()
    return _orchestrator


@app.get("/")
async def index():
    """Serve the frontend UI. Cache-Control: no-cache forces the browser to
    revalidate, so users see UI changes immediately after a sff update."""
    return FileResponse(
        str(frontend_dir / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.post("/api/sync")
async def start_sync():
    """Start a sync operation (runs in background)."""
    global _sync_task
    orchestrator = _get_orchestrator()

    if orchestrator.progress.status == "running":
        return JSONResponse({"error": "Sync already in progress"}, status_code=409)

    # Run sync in background thread
    loop = asyncio.get_event_loop()
    _sync_task = loop.run_in_executor(None, orchestrator.run_sync)

    return {"status": "started", "message": "Sync started"}


@app.post("/api/retry-failed")
async def retry_failed():
    """Re-attempt only the downloads that failed in earlier syncs (background)."""
    global _sync_task
    orchestrator = _get_orchestrator()

    if orchestrator.progress.status == "running":
        return JSONResponse({"error": "Sync already in progress"}, status_code=409)

    loop = asyncio.get_event_loop()
    _sync_task = loop.run_in_executor(None, orchestrator.run_retry_failed)

    return {"status": "started", "message": "Retrying failed downloads"}


@app.get("/api/status")
async def get_status():
    """Get current sync status and progress."""
    orchestrator = _get_orchestrator()
    return orchestrator.get_progress()


@app.get("/api/playlists")
async def get_playlists():
    """Get list of discovered FF playlists (from last sync)."""
    orchestrator = _get_orchestrator()
    return {
        "playlists": orchestrator.progress.playlist_details,
        "last_sync": orchestrator._state.get("last_sync"),
    }


SPOTIFY_CACHE_FILE = Path(__file__).parent / ".spotify_cache"


@app.get("/api/config")
async def get_config():
    """Get current configuration (non-sensitive). Resolves music_folder + traktor toggle via app_config."""
    from services import app_config, audio_formats
    from services import platform_paths
    cfg = app_config.load()
    traktor_nml = os.getenv("TRAKTOR_NML_PATH", platform_paths.DEFAULT_TRAKTOR_NML or "")
    return {
        "playlist_prefix": os.getenv("PLAYLIST_PREFIX", "FF"),
        "music_folder": app_config.get_music_folder(),
        "music_folder_set_by_user": cfg.get("music_folder") is not None,
        "first_run_complete": cfg.get("first_run_complete", False),
        "sync_to_traktor": app_config.get_sync_to_traktor(),
        "traktor_nml": traktor_nml,
        "traktor_nml_exists": bool(traktor_nml) and os.path.exists(traktor_nml),
        "rekordbox_db": platform_paths.REKORDBOX_MASTER_DB,
        "anlz_root": os.getenv("ANLZ_ROOT", platform_paths.DEFAULT_ANLZ_ROOT),
        "platform": "windows" if platform_paths.IS_WINDOWS else ("mac" if platform_paths.IS_MAC else "other"),
        "ffmpeg_available": platform_paths.FFMPEG_PATH is not None,
        "ffmpeg_path": platform_paths.FFMPEG_PATH,
        "spotify_signed_in": SPOTIFY_CACHE_FILE.exists(),
        "download_source": app_config.get_download_source(),
        "output_formats": app_config.get_output_formats(),
        "available_formats": [{"key": f.key, "label": f.label} for f in audio_formats.FORMATS.values()],
    }


@app.post("/api/spotify/sign-out")
async def spotify_sign_out():
    """Sign out of Spotify by deleting the cached OAuth token.

    Does NOT revoke the grant on Spotify's side — for a full revoke, the user
    has to visit https://www.spotify.com/account/apps. This just makes sff
    forget the current token so the next Sync triggers a fresh auth flow
    (e.g. to log in as a different account).
    """
    if SPOTIFY_CACHE_FILE.exists():
        try:
            SPOTIFY_CACHE_FILE.unlink()
            return {"status": "ok", "signed_in": False}
        except OSError as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return {"status": "ok", "signed_in": False, "note": "was not signed in"}


class MusicFolderUpdate(BaseModel):
    music_folder: str


class TraktorToggleUpdate(BaseModel):
    enabled: bool


class DownloadSourceUpdate(BaseModel):
    source: str  # "youtube" or "soulseek"


class OutputFormatsUpdate(BaseModel):
    formats: list[str]  # any of "flac", "aiff", "wav"


@app.post("/api/config/traktor")
async def set_traktor(payload: TraktorToggleUpdate):
    """Enable/disable Traktor sync. When enabled, each sync also writes to collection.nml."""
    from services import app_config
    cfg = app_config.set_sync_to_traktor(payload.enabled)
    return {"status": "ok", "sync_to_traktor": cfg["sync_to_traktor"]}


@app.post("/api/config/music-folder")
async def set_music_folder(payload: MusicFolderUpdate):
    """Set the music download folder. Appends '/Incoming' if not already present."""
    from services import app_config
    try:
        cfg = app_config.set_music_folder(payload.music_folder)
        return {"status": "ok", "music_folder": cfg["music_folder"]}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/config/download-source")
async def set_download_source(payload: DownloadSourceUpdate):
    """Choose whether the next Sync downloads via yt-dlp (MP3) or Soulseek (FLAC)."""
    from services import app_config
    try:
        cfg = app_config.set_download_source(payload.source)
        return {"status": "ok", "download_source": cfg["download_source"]}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/config/output-formats")
async def set_output_formats(payload: OutputFormatsUpdate):
    """Which lossless formats a Soulseek sync produces -- one Rekordbox playlist each."""
    from services import app_config
    try:
        cfg = app_config.set_output_formats(payload.formats)
        return {"status": "ok", "output_formats": app_config.get_output_formats()}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/soulseek/status")
async def soulseek_status():
    """Is Nicotine+ running, and is its API plugin reachable?"""
    from services import soulseek
    return soulseek.get_status()


@app.post("/api/soulseek/launch")
async def soulseek_launch():
    """Attempt to start Nicotine+ and wait for its API to come up."""
    from services import soulseek
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, soulseek.launch)
    return result


_PICK_FOLDER_SCRIPT = r"""
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as e:
    sys.stderr.write(f"tkinter unavailable: {e}\n")
    sys.exit(2)
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
try:
    chosen = filedialog.askdirectory(title="Choose your music download folder", mustexist=True)
finally:
    root.destroy()
sys.stdout.write(chosen or "")
"""


@app.post("/api/config/pick-folder")
async def pick_folder():
    """Open a native OS folder-picker dialog and return the selected path.

    Runs tkinter in a SUBPROCESS, not a worker thread — tkinter on macOS
    requires the main thread of its process, and uvicorn workers aren't it.
    The dialog appears on the user's machine because the server IS the user's machine.
    """
    import subprocess
    def _spawn():
        try:
            result = subprocess.run(
                [sys.executable, "-c", _PICK_FOLDER_SCRIPT],
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            return {"error": "Folder picker timed out"}
        if result.returncode == 2:
            return {"error": result.stderr.strip() or "tkinter not available — type the path manually"}
        if result.returncode != 0:
            return {"error": result.stderr.strip() or f"Picker exited {result.returncode}"}
        return {"path": result.stdout.strip()}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _spawn)
    if "error" in result:
        return JSONResponse(result, status_code=501)
    return result


@app.post("/api/playlists/{playlist_id}/stop-syncing")
async def stop_syncing_playlist(playlist_id: str):
    """Remove a playlist from sync_state.json. Does NOT touch Rekordbox/Traktor."""
    orchestrator = _get_orchestrator()
    result = orchestrator.stop_syncing_playlist(playlist_id)
    if not result.get("removed"):
        return JSONResponse(result, status_code=404)
    return result


@app.get("/api/playlists/tracked")
async def get_tracked_playlists():
    """List all playlists currently in sync_state.json (regardless of last sync run)."""
    orchestrator = _get_orchestrator()
    tracked = []
    for pid, pdata in orchestrator._state.get("playlists", {}).items():
        variants = {
            fmt: {
                "display_name": v.get("display_name"),
                "track_count": len(v.get("tracks", {})),
                "rb_playlist_id": v.get("rb_playlist_id"),
            }
            for fmt, v in (pdata.get("variants") or {}).items()
            if v.get("rb_playlist_id")
        }
        tracked.append({
            "id": pid,
            "name": pdata.get("name", pid),
            "display_name": pdata.get("display_name", pdata.get("name", pid)),
            "track_count": len(pdata.get("tracks", {})),
            "variants": variants,
        })
    return {"playlists": tracked}


@app.get("/api/health")
async def health():
    """Health check."""
    return {"status": "ok", "version": "1.0.0"}


# ─── Auto-shutdown when the browser disconnects ────────────────────────────
# Set AUTO_SHUTDOWN_IDLE=<seconds> to enable. start.command/start.bat set
# this so closing the browser tab also kills the server (and the Terminal
# window). Default off, so manual `python main.py` runs stay alive.

_AUTO_SHUTDOWN_IDLE = int(os.getenv("AUTO_SHUTDOWN_IDLE", "0"))
_last_heartbeat_at = time.monotonic()


@app.post("/api/heartbeat")
async def heartbeat():
    """Frontend pings this every few seconds while the page is open."""
    global _last_heartbeat_at
    _last_heartbeat_at = time.monotonic()
    return {"ok": True}


async def _idle_watchdog():
    """If no heartbeat for AUTO_SHUTDOWN_IDLE seconds, exit cleanly."""
    while True:
        await asyncio.sleep(5)
        idle = time.monotonic() - _last_heartbeat_at
        if idle > _AUTO_SHUTDOWN_IDLE:
            # Never kill the server mid-sync. A background browser tab gets its
            # timers throttled, so heartbeats stop long before a multi-hour
            # Soulseek sync finishes -- and shutting down here abandons the run.
            # Once the sync ends, the still-stale heartbeat shuts it down cleanly.
            if _orchestrator is not None and getattr(_orchestrator, "_running", False):
                continue
            logger.info("No browser heartbeat for %.0fs — auto-shutdown", idle)
            os.kill(os.getpid(), signal.SIGINT)
            return


@app.on_event("startup")
async def _start_idle_watchdog():
    if _AUTO_SHUTDOWN_IDLE > 0:
        logger.info("Auto-shutdown enabled: server exits if idle > %ds", _AUTO_SHUTDOWN_IDLE)
        asyncio.create_task(_idle_watchdog())


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8899"))
    logger.info("Starting Standalone File Fetcher on http://localhost:%d", port)
    logger.info("Music folder: %s", os.getenv("MUSIC_FOLDER", "not set"))
    logger.info("Playlist prefix: %s", os.getenv("PLAYLIST_PREFIX", "FF"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
