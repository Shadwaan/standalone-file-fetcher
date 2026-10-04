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
import shutil
from datetime import datetime
from pathlib import Path

from services import app_config, audio_formats, failure_log, soulseek, source_log, tagging, verify_audio
from services.labels import title_key
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

    # keyed ignoring a trailing "[version label]": a title we labelled is still the same track
    in_rekordbox = {title_key(t): p for t, p in (rb.get_playlist_track_paths(pid) if pid else {}).items()}
    known_ids = set(v["tracks"])

    # Rekordbox has it but our state never heard of it (e.g. a playlist built by
    # hand) -> record it instead of silently re-downloading everything.
    for t in tracks:
        if t.spotify_id not in known_ids and title_key(t.title) in in_rekordbox:
            path = in_rekordbox[title_key(t.title)]
            v["tracks"][t.spotify_id] = {
                "filename": Path(path).name if path else "", "file_path": path,
                "artist": t.artist, "title": t.title,
            }

    new_tracks = [t for t in tracks if t.spotify_id not in v["tracks"] and title_key(t.title) not in in_rekordbox]
    return _Variant(fmt, v, v["display_name"], new_tracks, known_ids - current_ids)


def _existing_sources(playlists_state: dict, track, library: list[tuple[str, str]],
                      siblings: dict[str, list[str]]) -> list[Path]:
    """Lossless files we ALREADY HAVE for this track, best first -- so it is never
    downloaded twice. Same idea as the YouTube path's dedup (Rekordbox library, then
    disk), with one difference: only lossless files count. An MP3 already in the
    library must not become the source of an "AIFF".

      1. what sff produced before, for this Spotify track in ANY playlist (the same
         track sitting in two playlists is one file, not two downloads)
      2. the same playlist in another format ("Deep tech FLAC" when making "Deep tech
         AIFF"), matched by title -- being in that playlist is enough evidence, even
         when the file's name doesn't contain the artist
      3. the Rekordbox library, matched by title + artist -- catches files sff's own
         state never heard of (built by hand, an older version, another tool)"""
    found: list[Path] = []

    def consider(path_str):
        if not path_str:
            return
        path = Path(path_str)
        if path not in found and path.suffix.lower() in audio_formats.LOSSLESS_EXTS and path.is_file()                 and audio_formats.is_valid_lossless(path):
            found.append(path)

    for pl in playlists_state.values():
        for v in pl.get("variants", {}).values():
            consider((v.get("tracks", {}).get(track.spotify_id) or {}).get("file_path"))

    for path_str in siblings.get(title_key(track.title), []):
        consider(path_str)

    title = track.title.strip().lower()
    first_artist = track.artist.split(",")[0].strip().lower()
    exact_stem = f"{track.artist} - {track.title}".lower()
    for lib_title, lib_path in library:
        stem = Path(lib_path).stem.lower()
        if stem == exact_stem or (lib_title.strip().lower() == title and first_artist in lib_path.lower()):
            consider(lib_path)

    found.sort(key=lambda p: soulseek.FORMAT_TIER.get(p.suffix.lower(), 0), reverse=True)
    return found


_AUDIO_EXTS = audio_formats.LOSSLESS_EXTS | {".mp3"}


def _references(needed, track_by_id, library) -> dict[str, Path]:
    """For each wanted track, a file we already hold that is the right song (e.g. the YouTube MP3 from
    before): the yardstick a download is compared with. Matched like _existing_sources (title + artist)."""
    out = {}
    for sid in needed:
        track = track_by_id[sid]
        title = track.title.strip().lower()
        first_artist = track.artist.split(",")[0].strip().lower()
        exact_stem = f"{track.artist} - {track.title}".lower()
        for lib_title, lib_path in library:
            path = Path(lib_path)
            if path.suffix.lower() not in _AUDIO_EXTS:
                continue
            if (path.stem.lower() == exact_stem or (lib_title.strip().lower() == title and first_artist in lib_path.lower())) \
                    and path.is_file():
                out[sid] = path
                break
    return out


def _make_validator(references: dict[str, Path]):
    """The "is this finished download any good?" gate resolve_all applies to every file that arrives.
    Returns True, or a short reason it was turned down (and another source is tried)."""
    def validate(path: Path, track):
        if path.suffix.lower() == ".mp3":
            verdict = soulseek.check_authenticity(path)
            if verdict["suspect"]:
                logger.info("rejected a fake 320 kbps MP3 (%s): %s", path.name, verdict["reason"])
                return "a fake 320 kbps MP3"
        elif not audio_formats.is_valid_lossless(path):
            return "not valid lossless audio"

        # a stem is never wanted unless the track itself is one
        if not soulseek.has_stem_marker(track.title) and verify_audio.looks_like_stem(path):
            logger.info("rejected %s: it has almost no bass, so it is a vocals-only stem", path.name)
            return "a vocals-only stem"

        reference = references.get(track.spotify_id)
        if reference and reference != path:
            similarity = verify_audio.compare(path, reference)
            if similarity is not None:
                if similarity < verify_audio.SAME_SONG_REJECT_BELOW:
                    logger.info("rejected %s: it does not match the copy you already have (%.2f)", path.name, similarity)
                    return f"a different song from the copy you already have (match {similarity:.2f})"
                if similarity < verify_audio.SAME_SONG_WARN_BELOW:
                    logger.warning("%s only partly matches the copy you already have (%.2f): a different version?",
                                   path.name, similarity)
        return True
    return validate


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
        if progress.tracks_deferred:
            progress.message += (f". {progress.tracks_deferred} tracks were skipped because they failed recently "
                                 f"-- press Retry failed to try them again")
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
    library = rb.get_library_files()
    siblings: dict[str, list[str]] = {}          # title -> files in this playlist's other-format twins
    for f in audio_formats.FORMATS.values():
        twin = rb.find_playlist_id(f"{base_name} {f.label}")
        for title, path in (rb.get_playlist_track_paths(twin) if twin else {}).items():
            siblings.setdefault(title_key(title), []).append(path)

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
        sources: dict = {}
        planned = _produce_files(orch, needed, track_by_id, variants, music_folder, nicotine_dir, library, siblings, sources)
        _write_to_rekordbox(orch, planned, variants, tracks, sources)

    pl_state["name"], pl_state["display_name"] = pl_name, base_name
    pl_state["snapshot_id"] = pl.get("snapshot_id", "")
    orch._save_state()      # after every playlist, so an interruption can't lose finished ones


def _produce_files(orch, needed, track_by_id, variants, music_folder, nicotine_dir, library, siblings, sources):
    """Stages 2 and 3: get a source for each track that needs one, then convert
    and tag it into every format it's missing. Returns [(spotify_id, fmt, path)].
    Never touches Rekordbox."""
    progress = orch.progress
    formats = list(variants)
    if not needed:
        return []

    # 2. SOURCE -- reuse what we already have before going to Soulseek
    existing = {sid: _existing_sources(orch._state["playlists"], track_by_id[sid], library, siblings) for sid in needed}
    derived = {sid: found[0] for sid, found in existing.items() if found}
    to_download = [track_by_id[sid] for sid in needed if sid not in derived]
    states = {}
    if to_download:
        def _cb(msg):
            progress.message = msg

        prefer = {"." + audio_formats.FORMATS[f].ext for f in formats}
        if "aiff" in formats:
            prefer.add(".aif")
        progress.phase = "searching"
        # Tracks that failed recently are not searched for again (unless "Retry failed" was pressed).
        cooling_down = set() if getattr(orch, "retry_recent_failures", False) else set(failure_log.recently_failed())
        states = soulseek.search_and_queue_all(to_download, on_progress=_cb, local_dir=nicotine_dir, prefer_exts=prefer,
                                               defer_ids=cooling_down)
        progress.phase = "downloading"
        def _notice(req):
            ask = f'reply "{req["phrase"]}"' if req["phrase"] else "reply to its message"
            progress.errors.append(
                f"Action needed from you: {req['user']} wants proof you are a person before it will send files -- "
                f"open Nicotine+ > Private Chat > {req['user']} and {ask} yourself. sff will not answer these for you.")

        soulseek.resolve_all(states, on_progress=_cb, download_dir=nicotine_dir,
                             validate=_make_validator(_references(needed, track_by_id, library)),
                             on_notice=_notice)

    # 3. FILE WORK
    progress.phase = "converting"
    planned = []
    failures: list[dict] = []
    for sid, fmts in needed.items():
        track = track_by_id[sid]
        label = f"{track.artist} - {track.title}"
        keep_source = sid in derived

        if keep_source:
            src = derived[sid]
            sources[sid] = {"user": "", "remote_path": str(src), "how": "made from a file we already had"}
        else:
            st = states.get(sid)
            src = soulseek.download_path_for(st, nicotine_dir) if st and st.downloaded else None
            if not src and st and st.deferred:
                progress.tracks_deferred += len(fmts)
                logger.info("not searching again for %s: it failed recently", label)
                continue
            if not src:
                progress.tracks_failed += len(fmts)
                progress.errors.append(f"Soulseek: no source found for {label} -- {st.why_no_source() if st else 'never searched'}")
                failures.append({"spotify_id": sid, "artist": track.artist, "title": track.title,
                                 "playlist": variants[fmts[0]].display_name,
                                 "category": st.failure_category() if st else "no results",
                                 "detail": st.why_no_source() if st else "never searched",
                                 "attempts": st.attempts if st else 0})
                continue
            progress.tracks_downloaded += 1
            user, remote = st.key if st and st.key else (None, None)
            if user:
                sources[sid] = {"user": user, "remote_path": remote, "how": "downloaded"}
                logger.info("downloaded %s from %s: %s", label, user, remote)
            else:
                sources[sid] = {"user": "", "remote_path": str(src), "how": "already in the Nicotine+ download folder"}
            if src.suffix.lower() in audio_formats.LOSSLESS_EXTS:
                auth = soulseek.check_authenticity(src)        # on the file as downloaded
                if auth["suspect"]:
                    msg = f"Suspect file (likely transcoded from a lossy source): {label} -- {auth['reason']}"
                    logger.warning(msg)
                    progress.errors.append(msg)

        # A 320 kbps MP3 only ever arrives as the fallback, when no lossless copy exists: keep it
        # as the MP3 it is. Converting it to AIFF/WAV/FLAC would triple its size and label lossy
        # audio as lossless, for no gain. It is tagged, given its cover, and shared by every
        # selected playlist as one file.
        if src.suffix.lower() == ".mp3":
            dest_dir = Path(music_folder) / variants[fmts[0]].display_name
            dest_dir.mkdir(parents=True, exist_ok=True)
            carry = tagging.read_carry_over_tags(src)
            art = tagging.fetch_artwork(track.artwork_url) or tagging.embedded_picture(src)
            dest = audio_formats.unique_path(dest_dir, src.stem, "mp3")
            if keep_source:
                shutil.copy2(src, dest)
            else:
                shutil.move(str(src), str(dest))
            if not tagging.write_tags(dest, track, art, carry):
                progress.errors.append(f"Could not write tags/cover for {label} (mp3); file kept as is")
            for fmt in fmts:
                planned.append((sid, fmt, dest))
            logger.info("kept %s as a 320 kbps MP3 (the lossless fallback): %s", label, dest.name)
            continue

        # A file that is already exactly the wanted format is reused in place (the same
        # physical file can sit in several Rekordbox playlists) -- no copy, no rewrite.
        for fmt in list(fmts):
            ready = next((p for p in existing.get(sid, []) if audio_formats.is_ready(p, fmt)), None)
            if ready:
                planned.append((sid, fmt, ready))
                progress.tracks_skipped += 1
                fmts.remove(fmt)
        if not fmts:
            continue

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
    # persistent records, so what Soulseek couldn't supply, and who supplied the rest, survive turning sff off
    failure_log.record(failures, resolved_ids={sid for sid, _, _ in planned})
    source_log.record([
        {"playlist": variants[fmt].display_name, "artist": track_by_id[sid].artist, "title": track_by_id[sid].title,
         "from_user": sources.get(sid, {}).get("user", ""), "remote_path": sources.get(sid, {}).get("remote_path", ""),
         "how": sources.get(sid, {}).get("how", "")}
        for sid, fmt, _ in planned if sid in sources])
    return planned


def _write_to_rekordbox(orch, planned, variants, tracks, sources) -> None:
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
                track_by_id[sid], file_extension=path.suffix.lstrip(".").lower() or audio_formats.FORMATS[fmt].ext,
                playlist_name=v.display_name)
            file_path = str(path).replace("\\", "/")
            content_id = rb.import_track_unanalyzed(file_path, track).get("id")
            if content_id and rb.add_track_to_playlist(pid, content_id, track.position + 1):
                progress.tracks_imported += 1
                v.state["tracks"][sid] = {
                    "filename": path.name, "file_path": file_path,
                    "artist": track.artist, "title": track.title,
                }
                if sid in sources:
                    v.state["tracks"][sid]["source"] = sources[sid]
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
