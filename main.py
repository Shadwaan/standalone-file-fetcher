"""
Standalone File Fetcher — FastAPI server.

Bridges Spotify playlists to Rekordbox and Traktor DJ libraries.
Run: python main.py
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Load environment
load_dotenv(Path(__file__).parent / ".env")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
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
    """Serve the frontend UI."""
    return FileResponse(str(frontend_dir / "index.html"))


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


@app.get("/api/config")
async def get_config():
    """Get current configuration (non-sensitive)."""
    return {
        "playlist_prefix": os.getenv("PLAYLIST_PREFIX", "FF"),
        "music_folder": os.getenv("MUSIC_FOLDER", ""),
        "rekordbox_db": os.getenv("REKORDBOX_DB_PATH", ""),
        "traktor_nml": os.getenv("TRAKTOR_NML_PATH", ""),
        "anlz_root": os.getenv("ANLZ_ROOT", ""),
    }


@app.get("/api/usb/drives")
async def get_usb_drives():
    """Detect connected USB drives."""
    from services.usb_detect import detect_usb_drives
    return {"drives": detect_usb_drives()}


_usb_export_task = None


@app.post("/api/usb/export")
async def start_usb_export(drive_letter: str = None):
    """Start USB export (runs in background)."""
    global _usb_export_task

    if not drive_letter:
        # Auto-select first USB drive
        from services.usb_detect import detect_usb_drives
        drives = detect_usb_drives()
        if not drives:
            return JSONResponse({"error": "No USB drive detected"}, status_code=400)
        drive_letter = drives[0]["letter"]

    from services.usb_export import export_to_usb, get_progress as usb_progress
    if usb_progress()["status"] == "running":
        return JSONResponse({"error": "USB export already in progress"}, status_code=409)

    loop = asyncio.get_event_loop()
    _usb_export_task = loop.run_in_executor(None, export_to_usb, drive_letter)

    return {"status": "started", "drive": drive_letter}


@app.get("/api/usb/status")
async def get_usb_status():
    """Get USB export progress."""
    from services.usb_export import get_progress as usb_progress
    return usb_progress()


@app.get("/api/health")
async def health():
    """Health check."""
    return {"status": "ok", "version": "1.0.0"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8899"))
    logger.info("Starting Standalone File Fetcher on http://localhost:%d", port)
    logger.info("Music folder: %s", os.getenv("MUSIC_FOLDER", "not set"))
    logger.info("Playlist prefix: %s", os.getenv("PLAYLIST_PREFIX", "FF"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
