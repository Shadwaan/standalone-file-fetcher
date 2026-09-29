"""
The Soulseek sync: find + download lossless files, then produce one Rekordbox
playlist per selected output format (FLAC / AIFF / WAV).

Stages, kept separate on purpose:

  1. READ ONLY   Work out, per format, what's missing (state file AND Rekordbox's
                 real playlist -- two independent checks).
  2. SOURCE      A track already in another format is *derived* from that file, with
                 no download. Only tracks with no file anywhere go to Soulseek.
  3. FILE WORK   Convert to every wanted format (16-bit, 44.1/48 kHz), tag with
                 Spotify's title/artist/album/year/cover. No Rekordbox involved.
  4. REKORDBOX   The only stage that writes to it. Runs once every download has
                 resolved, and waits for Rekordbox to be closed first.

Each Spotify playlist can have a playlist per format ("<name> FLAC", "<name> AIFF",
"<name> WAV"); re-syncing walks each selected format's playlist and adds only what
Spotify gained since.
"""

import dataclasses
import logging
import os
from datetime import datetime
from pathlib import Path

from services import app_config, audio_formats, soulseek, tagging
from services import rekordbox as rb

logger = logging.getLogger(__name__)


def migrate_state(state: dict) -> None:
    """Older state kept a single FLAC playlist per Spotify playlist under
    "flac_variant"; that's now one entry per format under "variants"."""
    for pl in state.get("playlists", {}).values():
        old = pl.pop("flac_variant", None)
        if old:
            pl.setdefault("variants", {}).setdefault("flac", old)


@dataclasses.dataclass
class _Variant:
    fmt: str
    state: dict            # this format's slice of the playlist's sync state
    display_name: str      # the Rekordbox playlist's actual name
    new_tracks: list
    removed_ids: set


def _new_variant_state() -> dict:
    return {"tracks": {}, "rb_playlist_id": None, "display_name": None, "created_at": None}


def _inspect_variant(pl_state: dict, fmt: str, base_name: str, tracks: list, current_ids: set) -> _Variant:
    """Stage 1, read-only: what does this format's playlist still need?"""
    label = audio_formats.FORMATS[fmt].label
    wanted = f"{base_name} {label}"
    v = pl_state.setdefault("variants", {}).setdefault(fmt, _new_variant_state())

    # Trust a stored playlist ID only if it still resolves; otherwise look the
    # playlist up by name (case-insensitively, so one made by hand is reused).
    pid = v.get("rb_playlist_id")
    name = rb.get_playlist_name(pid) if pid else None
    if not name:
        pid = rb.find_playlist_id(wanted)
        name = rb.get_playlist_name(pid) if pid else None
    v["rb_playlist_id"] = pid
    v["display_name"] = name or wanted

    in_rekordbox = rb.get_playlist_track_paths(pid) if pid else {}
    known_ids = set(v["tracks"])

    # Rekordbox has it but our state never heard of it (e.g. a playlist built by
    # hand) -> record it instead of silently re-downloading everything.
    for t in tracks:
        if t.spotify_id not in known_ids and t.title in in_rekordbox:
            path = in_rekordbox[t.title]
            v["tracks"][t.spotify_id] = {
                "filename": Path(path).name if path else "", "file_path": path,
                "artist": t.artist, "title": t.title,
            }

    new_tracks = [t for t in tracks if t.spotify_id not in v["tracks"] and t.title not in in_rekordbox]
    return _Variant(fmt, v, v["display_name"], new_tracks, known_ids - current_ids)


def _existing_output(pl_state: dict, spotify_id: str) -> Path | None:
    """A file we already produced for this track in ANY format -- a valid source
    for deriving a newly-wanted format without downloading again."""
    for v in pl_state.get("variants", {}).values():
        path = (v.get("tracks", {}).get(spotify_id) or {}).get("file_path")
        if path and Path(path).is_file():
            return Path(path)
    return None


def _usable_download(path: Path) -> bool:
    return path.suffix.lower() == ".mp3" or audio_formats.is_valid_lossless(path)


def run_soulseek_sync(orch) -> dict:
    """Run the Soulseek sync on a SyncOrchestrator (it owns state and progress)."""
    if orch._refuse_if_rekordbox_running():
        return orch.get_progress()

    progress = orch.progress
    status = soulseek.get_status()
    if not status["running"]:
        progress.phase = "starting_nicotine"
        progress.message = "Nicotine+ isn't running -- starting it..."
        launched = soulseek.launch()
        if not launched["api_reachable"]:
            progress.status = "error"
            progress.message = launched["error"] or (
                "Nicotine+ needs to be running for Soulseek downloads. "
                "Please start it and enable the 'API Nicotine Plus' plugin.")
            progress.errors.append(progress.message)
            return orch.get_progress()
    elif not status["api_reachable"]:
        progress.status = "error"
        progress.message = ("Nicotine+ is running but its API plugin isn't reachable. "
                            "Enable 'API Nicotine Plus' in Preferences -> Plugins.")
        progress.errors.append(progress.message)
        return orch.get_progress()

    from services.spotify import SpotifyService

    formats = app_config.get_output_formats()
    music_folder = app_config.get_music_folder()
    nicotine_dir = os.getenv("NICOTINE_DOWNLOAD_DIR", r"D:\Music\Nicotine")

    progress.phase = "discovering"
    progress.message = "Connecting to Spotify..."
    logger.info("=== Soulseek sync started (formats: %s) ===", ", ".join(formats))

    spotify = SpotifyService()
    playlists = spotify.get_prefixed_playlists()
    progress.playlists_found = len(playlists)

    try:
        for pl in playlists:
            _sync_playlist(orch, spotify, pl, formats, music_folder, nicotine_dir)

        progress.status = "done"
        progress.phase = "complete"
        labels = "/".join(audio_formats.FORMATS[f].label for f in formats)
        progress.message = (f"{labels} sync complete: {progress.tracks_imported} imported, "
                            f"{progress.tracks_failed} failed, {progress.tracks_removed} removed from playlists")
        logger.info("=== %s ===", progress.message)
    finally:
        orch._save_state()
    return orch.get_progress()


def _sync_playlist(orch, spotify, pl: dict, formats: list[str], music_folder: str, nicotine_dir: str) -> None:
    progress = orch.progress
    pl_id, pl_name, base_name = pl["id"], pl["name"], pl["display_name"]

    progress.phase = "syncing"
    progress.message = f"Processing playlist: {pl_name}"
    logger.info("Processing playlist: %s", pl_name)

    tracks = spotify.get_playlist_tracks(pl_id, pl_name)
    track_by_id = {t.spotify_id: t for t in tracks}
    current_ids = set(track_by_id)

    pl_state = orch._state["playlists"].setdefault(
        pl_id, {"tracks": {}, "snapshot_id": "", "name": pl_name, "display_name": base_name})

    # ── 1. read only ────────────────────────────────────────────────────────
    # Reads still wait if Rekordbox is open: a failed read looks like "nothing in
    # the library yet" and would re-download everything.
    orch._wait_until_rekordbox_closed("checking what's already in your library")
    variants = {fmt: _inspect_variant(pl_state, fmt, base_name, tracks, current_ids) for fmt in formats}

    needed: dict[str, list[str]] = {}      # spotify_id -> formats still missing for it
    for fmt in formats:
        v = variants[fmt]
        progress.playlist_details.append({
            "name": pl_name, "display_name": v.display_name,
            "total": len(tracks), "new": len(v.new_tracks), "removed": len(v.removed_ids),
        })
        progress.tracks_total += len(v.new_tracks)
        for t in v.new_tracks:
            needed.setdefault(t.spotify_id, []).append(fmt)

    if needed or any(v.removed_ids for v in variants.values()):
        planned = _produce_files(orch, needed, track_by_id, variants, pl_state, music_folder, nicotine_dir)
        _write_to_rekordbox(orch, planned, variants, tracks)

    pl_state["name"], pl_state["display_name"] = pl_name, base_name
    pl_state["snapshot_id"] = pl.get("snapshot_id", "")
    orch._save_state()      # after every playlist, so an interruption can't lose finished ones


def _produce_files(orch, needed, track_by_id, variants, pl_state, music_folder, nicotine_dir):
    """Stages 2 and 3: get a source for each track that needs one, then convert
    and tag it into every format it's missing. Returns [(spotify_id, fmt, path)].
    Never touches Rekordbox."""
    progress = orch.progress
    formats = list(variants)
    if not needed:
        return []

    # 2. SOURCE -- reuse what we already have before going to Soulseek
    derived = {sid: p for sid in needed if (p := _existing_output(pl_state, sid))}
    to_download = [track_by_id[sid] for sid in needed if sid not in derived]
    states = {}
    if to_download:
        def _cb(msg):
            progress.message = msg

        prefer = {"." + audio_formats.FORMATS[f].ext for f in formats}
        if "aiff" in formats:
            prefer.add(".aif")
        progress.phase = "searching"
        states = soulseek.search_and_queue_all(to_download, on_progress=_cb, local_dir=nicotine_dir, prefer_exts=prefer)
        progress.phase = "downloading"
        soulseek.resolve_all(states, on_progress=_cb, download_dir=nicotine_dir, validate=_usable_download)

    # 3. FILE WORK
    progress.phase = "converting"
    planned = []
    for sid, fmts in needed.items():
        track = track_by_id[sid]
        label = f"{track.artist} - {track.title}"
        keep_source = sid in derived

        if keep_source:
            src = derived[sid]
        else:
            st = states.get(sid)
            src = soulseek.download_path_for(st, nicotine_dir) if st and st.downloaded else None
            if not src:
                progress.tracks_failed += len(fmts)
                progress.errors.append(f"Soulseek: no source found for {label}")
                continue
            progress.tracks_downloaded += 1
            if src.suffix.lower() in audio_formats.LOSSLESS_EXTS:
                auth = soulseek.check_authenticity(src)        # on the file as downloaded
                if auth["suspect"]:
                    msg = f"Suspect file (likely transcoded from a lossy source): {label} -- {auth['reason']}"
                    logger.warning(msg)
                    progress.errors.append(msg)

        # everything read from the source happens BEFORE it can be moved
        carry = tagging.read_carry_over_tags(src)
        art = tagging.fetch_artwork(track.artwork_url) or tagging.embedded_picture(src)

        first_dir = Path(music_folder) / variants[fmts[0]].display_name
        outputs = audio_formats.build_outputs(
            src, fmts, lambda f: Path(music_folder) / variants[f].display_name,
            first_dir / "_originals", keep_source=keep_source)

        for fmt in fmts:
            path = outputs.get(fmt)
            if not path:
                progress.tracks_failed += 1
                progress.errors.append(f"Could not convert to {audio_formats.FORMATS[fmt].label}: {label}")
                continue
            if not tagging.write_tags(path, track, art, carry):
                progress.errors.append(f"Could not write tags/cover for {label} ({fmt}); file kept as is")
            planned.append((sid, fmt, path))
    return planned


def _write_to_rekordbox(orch, planned, variants, tracks) -> None:
    """Stage 4: the only place that writes to Rekordbox."""
    progress = orch.progress
    if not planned and not any(v.removed_ids for v in variants.values()):
        return

    # The sync may have run for hours: "was it closed when we started?" proves
    # nothing now, so wait here rather than write into an open database.
    orch._wait_until_rekordbox_closed("tracks are ready to import")
    progress.phase = "importing"
    track_by_id = {t.spotify_id: t for t in tracks}
    ordered_titles = [t.title for t in tracks]

    for fmt, v in variants.items():
        mine = [(sid, path) for sid, f, path in planned if f == fmt]
        if not mine and not v.removed_ids:
            continue

        pid = v.state.get("rb_playlist_id") or rb.find_or_create_playlist(v.display_name)
        if not pid:
            progress.errors.append(f"Could not create the Rekordbox playlist '{v.display_name}'")
            progress.tracks_failed += len(mine)
            continue
        v.state["rb_playlist_id"] = pid
        v.state["created_at"] = v.state.get("created_at") or datetime.now().isoformat()

        for sid, path in mine:
            track = dataclasses.replace(
                track_by_id[sid], file_extension=audio_formats.FORMATS[fmt].ext, playlist_name=v.display_name)
            file_path = str(path).replace("\\", "/")
            content_id = rb.import_track_unanalyzed(file_path, track).get("id")
            if content_id and rb.add_track_to_playlist(pid, content_id, track.position + 1):
                progress.tracks_imported += 1
                v.state["tracks"][sid] = {
                    "filename": path.name, "file_path": file_path,
                    "artist": track.artist, "title": track.title,
                }
            else:
                progress.tracks_failed += 1
                progress.errors.append(f"Rekordbox import failed: {track.artist} - {track.title} ({fmt})")

        # Removals: out of the playlist only -- never delete files
        for rid in v.removed_ids:
            filename = (v.state["tracks"].get(rid) or {}).get("filename", "")
            if filename:
                rb.remove_track_from_playlist(v.display_name, filename)
                progress.tracks_removed += 1
            v.state["tracks"].pop(rid, None)

        # Renumber from true Spotify order every time, so a late-arriving track
        # can't leave a stale TrackNo colliding with another's.
        rb.reorder_playlist_by_titles(pid, ordered_titles)

    rb.flush_wal()
