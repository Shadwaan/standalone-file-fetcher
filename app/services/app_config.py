"""
User-facing app configuration (app_config.json).

Persisted preferences set via the UI: music folder location, first-run state.
Distinct from .env (which holds secrets) and sync_state.json (per-playlist sync tracking).

Resolution priority for music_folder (highest first):
  1. app_config.json -> "music_folder"  (set via UI)
  2. MUSIC_FOLDER env var               (legacy / power-user override)
  3. platform_paths.DEFAULT_MUSIC_FOLDER  (~/Music/Incoming on Mac, %USERPROFILE%/Music/Incoming on Windows)
"""

import json
import logging
import os
import tempfile
from pathlib import Path

from services.platform_paths import DEFAULT_MUSIC_FOLDER

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent.parent / "app_config.json"

_DEFAULTS = {
    "music_folder": None,        # null = unconfigured; UI shows first-run prompt
    "first_run_complete": False,
}


def load() -> dict:
    """Load app config, falling back to defaults for any missing keys."""
    if not CONFIG_FILE.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        merged = dict(_DEFAULTS)
        merged.update({k: v for k, v in data.items() if k in _DEFAULTS})
        return merged
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("app_config.json unreadable (%s) — using defaults", e)
        return dict(_DEFAULTS)


def save(config: dict) -> None:
    """Atomically write app config to disk."""
    valid = {k: v for k, v in config.items() if k in _DEFAULTS}
    fd, tmp_path = tempfile.mkstemp(dir=str(CONFIG_FILE.parent), prefix=".app_config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(valid, f, indent=2)
        os.replace(tmp_path, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_music_folder() -> str:
    """Resolve the effective music folder (UI > env > platform default)."""
    cfg = load()
    if cfg.get("music_folder"):
        return cfg["music_folder"]
    env_val = os.getenv("MUSIC_FOLDER")
    if env_val:
        return env_val
    return DEFAULT_MUSIC_FOLDER


def set_music_folder(folder: str) -> dict:
    """Set the music folder, append /Incoming if not already present, mark first-run done.

    Returns the updated config dict.
    """
    folder = os.path.expanduser(folder.strip()) if folder else ""
    if not folder:
        raise ValueError("music_folder cannot be empty")
    if not os.path.isabs(folder):
        raise ValueError(f"music_folder must be an absolute path, got: {folder}")
    # Append /Incoming if the user picked a parent directory
    if os.path.basename(folder).lower() != "incoming":
        folder = os.path.join(folder, "Incoming")
    cfg = load()
    cfg["music_folder"] = folder
    cfg["first_run_complete"] = True
    save(cfg)
    logger.info("music_folder set to %s", folder)
    return cfg


def is_first_run() -> bool:
    """True if the user hasn't completed first-run setup yet."""
    return not load().get("first_run_complete", False)
