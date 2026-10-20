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
    "sync_to_traktor": False,    # also write to Traktor's collection.nml on each sync
    "download_source": "youtube",  # "youtube" (yt-dlp, MP3) or "soulseek" (Nicotine+, lossless)
    "output_formats": ["flac"],    # Soulseek only: any of "flac", "aiff", "wav" -- one playlist each
    "extra_playlists": [],         # other people's playlists to keep in sync: [{"id", "name", "display_name"}]
    "skip_playlists_once": [],     # playlist names (as shown in Rekordbox, without the format) left out of the NEXT sync only
    "failed_cooldown_hours": None, # for ONE run: treat tracks that failed within this many hours as already checked
    "youtube_fallback": False,     # Soulseek only: fill a track nothing else could supply with a labelled YouTube copy
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


def get_sync_to_traktor() -> bool:
    """Resolve whether to also write to Traktor (UI checkbox > env override > False)."""
    cfg = load()
    if cfg.get("sync_to_traktor"):
        return True
    # Legacy env var override (for users upgrading from earlier versions)
    return os.getenv("ENABLE_TRAKTOR", "0").lower() in ("1", "true", "yes", "on")


def set_sync_to_traktor(enabled: bool) -> dict:
    """Set the Traktor sync toggle. Returns the updated config dict."""
    cfg = load()
    cfg["sync_to_traktor"] = bool(enabled)
    save(cfg)
    logger.info("sync_to_traktor set to %s", cfg["sync_to_traktor"])
    return cfg


def get_skip_playlists_once() -> list[str]:
    return [str(n).strip().lower() for n in (load().get("skip_playlists_once") or []) if str(n).strip()]


def get_failed_cooldown_hours() -> float | None:
    value = load().get("failed_cooldown_hours")
    return float(value) if value else None


def get_youtube_fallback() -> bool:
    return bool(load().get("youtube_fallback", False))


def set_youtube_fallback(enabled: bool) -> dict:
    cfg = load()
    cfg["youtube_fallback"] = bool(enabled)
    save(cfg)
    logger.info("youtube_fallback set to %s", cfg["youtube_fallback"])
    return cfg


def get_extra_playlists() -> list[dict]:
    """Playlists made by other people that are synced alongside the prefixed ones."""
    items = load().get("extra_playlists") or []
    return [i for i in items if isinstance(i, dict) and i.get("id")]


def add_extra_playlist(playlist_id: str, name: str, display_name: str = "") -> dict:
    """Follow another playlist (by Spotify id). Adding the same id again just updates its names."""
    if not playlist_id:
        raise ValueError("a playlist id is required")
    cfg = load()
    items = [i for i in (cfg.get("extra_playlists") or []) if i.get("id") != playlist_id]
    entry = {"id": playlist_id, "name": name, "display_name": (display_name or name).strip()}
    items.append(entry)
    cfg["extra_playlists"] = items
    save(cfg)
    logger.info("now following playlist %s (%s)", name, playlist_id)
    return entry


def remove_extra_playlist(playlist_id: str) -> bool:
    cfg = load()
    items = cfg.get("extra_playlists") or []
    kept = [i for i in items if i.get("id") != playlist_id]
    if len(kept) == len(items):
        return False
    cfg["extra_playlists"] = kept
    save(cfg)
    return True


def get_download_source() -> str:
    """'youtube' (yt-dlp, MP3) or 'soulseek' (Nicotine+, FLAC)."""
    cfg = load()
    return cfg.get("download_source", "youtube")


def set_download_source(source: str) -> dict:
    """Set the download source. Returns the updated config dict."""
    if source not in ("youtube", "soulseek"):
        raise ValueError(f"download_source must be 'youtube' or 'soulseek', got: {source}")
    cfg = load()
    cfg["download_source"] = source
    save(cfg)
    logger.info("download_source set to %s", source)
    return cfg


def get_output_formats() -> list[str]:
    """The lossless formats a Soulseek sync produces (each gets its own playlist).
    Always a non-empty list in canonical order; falls back to FLAC."""
    from services.audio_formats import FORMAT_ORDER
    chosen = load().get("output_formats") or []
    valid = [f for f in FORMAT_ORDER if f in chosen]
    return valid or ["flac"]


def set_output_formats(formats: list[str]) -> dict:
    """Set the output formats. Rejects unknown names and an empty selection --
    a Soulseek sync with nothing to produce makes no sense. Returns the config."""
    from services.audio_formats import FORMAT_ORDER
    formats = list(formats or [])
    unknown = [f for f in formats if f not in FORMAT_ORDER]
    if unknown:
        raise ValueError(f"unknown output format(s): {unknown}; choose from {FORMAT_ORDER}")
    if not formats:
        raise ValueError("choose at least one output format")
    cfg = load()
    cfg["output_formats"] = [f for f in FORMAT_ORDER if f in formats]
    save(cfg)
    logger.info("output_formats set to %s", cfg["output_formats"])
    return cfg

