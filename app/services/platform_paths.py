"""
Platform-specific path defaults and tool locations.

Single source of truth for "where does Rekordbox live", "where's ffmpeg",
"where should we download MP3s by default", etc. Every other module imports
from here instead of inlining sys.platform checks.

User-supplied values (env vars or app_config.json) always take precedence
over these defaults.
"""

import glob
import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _rekordbox_base_dir() -> str:
    """Root of the Rekordbox config/library directory."""
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        return os.path.join(appdata, "Pioneer", "rekordbox")
    if IS_MAC:
        return str(Path.home() / "Library" / "Pioneer" / "rekordbox")
    return str(Path.home() / ".config" / "Pioneer" / "rekordbox")


REKORDBOX_BASE_DIR = _rekordbox_base_dir()
REKORDBOX_MASTER_DB = os.path.join(REKORDBOX_BASE_DIR, "master.db")
REKORDBOX_WAL = os.path.join(REKORDBOX_BASE_DIR, "master.db-wal")
REKORDBOX_SHARE_DIR = os.path.join(REKORDBOX_BASE_DIR, "share")
REKORDBOX_PLAYLISTS_XML = os.path.join(REKORDBOX_BASE_DIR, "masterPlaylists6.xml")
DEFAULT_ANLZ_ROOT = os.path.join(REKORDBOX_SHARE_DIR, "PIONEER", "USBANLZ")


def _default_traktor_nml() -> str:
    """Best-guess Traktor collection.nml path. Globs for any installed Traktor version."""
    if IS_WINDOWS:
        candidates = glob.glob(str(Path.home() / "Documents" / "Native Instruments" / "Traktor*" / "collection.nml"))
    elif IS_MAC:
        candidates = glob.glob(
            str(Path.home() / "Library" / "Application Support" / "Native Instruments" / "Traktor*" / "collection.nml")
        )
    else:
        candidates = []
    if candidates:
        return sorted(candidates)[-1]  # newest version wins lexicographically
    if IS_WINDOWS:
        return str(Path.home() / "Documents" / "Native Instruments" / "Traktor 3.8.0" / "collection.nml")
    if IS_MAC:
        return str(Path.home() / "Library" / "Application Support" / "Native Instruments" / "Traktor 3.8.0" / "collection.nml")
    return ""


DEFAULT_TRAKTOR_NML = _default_traktor_nml()


def _default_music_folder() -> str:
    """Where downloads land by default. User can override via UI/env."""
    return str(Path.home() / "Music" / "Incoming")


DEFAULT_MUSIC_FOLDER = _default_music_folder()


def _resolve_ffmpeg() -> str | None:
    """Locate ffmpeg. Prefer full builds (with libmp3lame) over PATH stubs.

    On Windows, the App Execution Alias at
    ``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\ffmpeg.EXE`` typically appears
    on PATH before WinGet's install dir. That alias is often an
    **audio-only** ffmpeg build that can decode MP3 but CANNOT encode it
    (no libmp3lame in its configure flags). yt-dlp's MP3 320kbps conversion
    fails against it with "Encoder not found".

    We therefore check WinGet's Gyan.FFmpeg full build FIRST on Windows
    (which README step 2 tells users to install), then fall back to PATH.
    On Mac, brew's ffmpeg includes libmp3lame by default, so the original
    PATH-first order is fine there.
    """
    if IS_WINDOWS:
        candidates = glob.glob(
            str(Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
                / "Gyan.FFmpeg*" / "ffmpeg-*-full_build" / "bin" / "ffmpeg.exe")
        )
        if candidates:
            return sorted(candidates)[-1]
    found = shutil.which("ffmpeg")
    if found:
        return found
    if IS_MAC:
        # Standard brew prefixes + common non-default user-local installs
        candidates = [
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            str(Path.home() / ".local/homebrew/bin/ffmpeg"),
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
    return None


FFMPEG_PATH = _resolve_ffmpeg()
FFMPEG_DIR = os.path.dirname(FFMPEG_PATH) if FFMPEG_PATH else None


REKORDBOX_PROCESS_NAMES = {"rekordbox.exe", "rekordbox"}


def is_rekordbox_running() -> bool:
    """True if any Rekordbox process is currently running (cross-platform)."""
    import psutil
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name in REKORDBOX_PROCESS_NAMES:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


NICOTINE_PROCESS_NAMES = {"nicotine+.exe", "nicotine+-debug.exe", "nicotine+", "nicotine"}
NICOTINE_API_BASE_URL = os.getenv("NICOTINE_API_BASE_URL", "http://127.0.0.1:12339")


def _default_nicotine_exe() -> str | None:
    """Best-guess Nicotine+ executable path. User can override via NICOTINE_EXE_PATH."""
    override = os.getenv("NICOTINE_EXE_PATH")
    if override and os.path.exists(override):
        return override
    if IS_WINDOWS:
        candidates = [
            r"D:\Program Files\Nicotine+\Nicotine+.exe",
            r"C:\Program Files\Nicotine+\Nicotine+.exe",
            r"C:\Program Files (x86)\Nicotine+\Nicotine+.exe",
        ]
        candidates += glob.glob(str(Path.home() / "AppData" / "Local" / "Programs" / "Nicotine+" / "Nicotine+.exe"))
        for c in candidates:
            if os.path.exists(c):
                return c
    found = shutil.which("nicotine+") or shutil.which("nicotine")
    return found


DEFAULT_NICOTINE_EXE = _default_nicotine_exe()


def is_nicotine_running() -> bool:
    """True if any Nicotine+ process is currently running (cross-platform)."""
    import psutil
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name in NICOTINE_PROCESS_NAMES:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False
