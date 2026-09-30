"""
End-to-end test of the Soulseek sync, with Nicotine+, Spotify and Rekordbox faked
and everything else real: ffmpeg conversion, tagging, the watchdog, state handling.

Nothing here touches a real Rekordbox library, Soulseek, or your sync_state.json.
Needs ffmpeg/ffprobe on PATH.   Run:   python -m unittest discover -s tests -v
"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from models.track import TrackInfo  # noqa: E402
from services import app_config, audio_formats, soulseek, sync, tagging  # noqa: E402
from services import rekordbox as rb  # noqa: E402
import mutagen  # noqa: E402


# ─── fixtures: real audio, faked network/database ────────────────────────────

def make_audio(path: Path, kind: str) -> None:
    """Write a 2-second stereo test file. kind: flac24_96 | flac16 | wav16 | adpcm | lossy16 (white noise steeply low-passed at 16 kHz, like a 128 kbps MP3)"""
    src = {"lossy16": "anoisesrc=d=2:c=white:a=0.3:r=44100"}.get(
        kind, "anoisesrc=d=2:c=pink:a=0.3:r=%d" % (96000 if kind == "flac24_96" else 44100))
    steep = ",lowpass=f=16000:p=2" * 6 if kind == "lossy16" else ""
    fmt = {"flac24_96": "s32", "flac16": "s16", "wav16": "s16", "lossy16": "s16", "adpcm": "s16"}[kind]
    codec = {"flac24_96": "flac", "flac16": "flac", "lossy16": "flac", "wav16": "pcm_s16le", "adpcm": "adpcm_ms"}[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-ac", "2",
                    "-af", f"aformat=sample_fmts={fmt}{steep}", "-c:a", codec, str(path)],
                   capture_output=True, check=True)


def pcm_md5(path: Path) -> str:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c:a", "pcm_s16le",
                           "-f", "md5", "-"], capture_output=True, text=True).stdout.strip()


class FakeNicotine:
    """Search results are looked up by query; 'downloading' materialises the file
    immediately and reports it Finished."""

    def __init__(self, download_dir: Path, events: list):
        self.dir, self.events = download_dir, events
        self.catalog: dict[str, list[dict]] = {}
        self.searches: list[str] = []
        self.downloads: list[dict] = []

    def offer(self, artist, title, kind, user, name, attrs, free=True, speed=1000):
        query = soulseek._build_query(artist, title)
        self.catalog.setdefault(query, []).append({
            "username": user, "file_path": name, "size": 1000, "file_attributes": attrs,
            "free_upload_slots": free, "upload_speed": speed, "_kind": kind})

    def api_post(self, path, payload):
        self.searches.append(payload["query"])
        self.events.append(("search", payload["query"]))
        return {"token": len(self.searches)}

    def fetch_all_results(self, token, max_offset=4000):
        return list(self.catalog.get(self.searches[token - 1], []))

    def get_downloads(self, active_only=False):
        return list(self.downloads)

    def enqueue(self, item):
        name = item["file_path"].replace("\\", "/").split("/")[-1]
        make_audio(self.dir / name, item["_kind"])
        self.events.append(("enqueue", name))
        self.downloads.append({"username": item["username"], "virtual_path": item["file_path"],
                               "status": "Finished", "progress_pct": 100.0})
        return {"ok": True}


class FakeRekordbox:
    def __init__(self, events: list):
        self.events = events
        self.playlists: dict[str, dict] = {}     # id -> {"name", "tracks": [content ids]}
        self.contents: dict[str, dict] = {}
        self.n = 1000
        self.imports, self.removed, self.reorders, self.flushes = [], [], [], 0

    def _id(self):
        self.n += 1
        return str(self.n)

    def add_existing_playlist(self, name, tracks: dict[str, str]):
        pid = self._id()
        self.playlists[pid] = {"name": name, "tracks": []}
        for title, path in tracks.items():
            cid = self._id()
            self.contents[cid] = {"title": title, "path": path}
            self.playlists[pid]["tracks"].append(cid)
        return pid

    def find_playlist_id(self, name):
        for pid, p in self.playlists.items():
            if p["name"] == name:
                return pid
        for pid, p in self.playlists.items():
            if p["name"].lower() == name.lower():
                return pid
        return None

    def get_playlist_name(self, pid):
        return self.playlists.get(pid, {}).get("name")

    def get_library_files(self):
        return [(c["title"], c["path"]) for c in self.contents.values()]

    def get_playlist_track_paths(self, pid):
        return {self.contents[c]["title"]: self.contents[c]["path"] for c in self.playlists.get(pid, {}).get("tracks", [])}

    def find_or_create_playlist(self, name):
        for pid, p in self.playlists.items():
            if p["name"] == name:
                return pid
        pid = self._id()
        self.playlists[pid] = {"name": name, "tracks": []}
        self.events.append(("create_playlist", name))
        return pid

    def import_track_unanalyzed(self, path, track):
        self.events.append(("import", track.title, track.file_extension))
        cid = self._id()
        self.contents[cid] = {"title": track.title, "path": path}
        self.imports.append((track, path))
        return {"status": "imported", "id": cid}

    def add_track_to_playlist(self, pid, cid, n):
        self.playlists[pid]["tracks"].append(cid)
        return True

    def remove_track_from_playlist(self, name, filename):
        for p in self.playlists.values():
            if p["name"] == name:
                for c in list(p["tracks"]):
                    if self.contents[c]["path"].endswith(filename):
                        p["tracks"].remove(c)
                        self.removed.append((name, filename))
                        return True
        return False

    def reorder_playlist_by_titles(self, pid, titles):
        self.reorders.append((pid, list(titles)))
        return 0

    def flush_wal(self):
        self.flushes += 1


def spotify_track(n, sid, title, artist, position, year="2020"):
    return TrackInfo(spotify_id=sid, title=title, artist=artist, album=f"{title} EP", year=year,
                     duration_ms=1, artwork_url=None, playlist_name="x", position=position)


class SyncFixture(unittest.TestCase):
    """Temp dirs, faked Nicotine+/Spotify/Rekordbox, and helpers. No tests of its own."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.music, self.nic = self.tmp / "Incoming", self.tmp / "Nicotine"
        self.music.mkdir(); self.nic.mkdir()
        self.events: list[tuple] = []

        # config + state live in the temp dir, never the real ones
        mock.patch.object(app_config, "CONFIG_FILE", self.tmp / "app_config.json").start()
        mock.patch.object(sync, "STATE_FILE", self.tmp / "sync_state.json").start()
        app_config.save({"music_folder": str(self.music), "download_source": "soulseek", "output_formats": ["flac", "aiff"]})
        mock.patch.dict("os.environ", {"NICOTINE_DOWNLOAD_DIR": str(self.nic)}).start()

        self.nico = FakeNicotine(self.nic, self.events)
        self.rbfake = FakeRekordbox(self.events)
        for name, attr in [("_api_post", self.nico.api_post), ("_fetch_all_results", self.nico.fetch_all_results),
                           ("get_downloads", self.nico.get_downloads), ("enqueue", self.nico.enqueue),
                           ("is_connected", lambda: True),
                           ("get_status", lambda: {"running": True, "api_reachable": True})]:
            mock.patch.object(soulseek, name, attr).start()
        mock.patch("time.sleep", lambda s: None).start()
        for name in ("find_playlist_id", "get_playlist_name", "get_playlist_track_paths", "find_or_create_playlist",
                     "import_track_unanalyzed", "add_track_to_playlist", "get_library_files", "remove_track_from_playlist",
                     "reorder_playlist_by_titles", "flush_wal"):
            mock.patch.object(rb, name, getattr(self.rbfake, name)).start()

        self.spotify_tracks = []
        self.playlist_names = {"PL1": "Deep tech"}
        outer = self

        class FakeSpotify:
            def get_prefixed_playlists(self):
                return [{"id": pid, "name": f"FF {name}", "display_name": name, "snapshot_id": "s1"}
                        for pid, name in outer.playlist_names.items()]

            def get_playlist_tracks(self, pid, name):
                return [spotify_track(0, t.spotify_id, t.title, t.artist, i) for i, t in enumerate(outer.spotify_tracks)]

        mock.patch("services.spotify.SpotifyService", FakeSpotify).start()
        self.addCleanup(mock.patch.stopall)

    # ── helpers ──
    def orchestrator(self):
        orch = sync.SyncOrchestrator()
        orch._is_rekordbox_running = lambda: False
        original = orch._wait_until_rekordbox_closed
        orch._wait_until_rekordbox_closed = lambda reason: (self.events.append(("wait", reason)), original(reason))[1]
        return orch

    def run_sync(self, formats=None):
        if formats:
            app_config.set_output_formats(formats)
        result = self.orchestrator().run_sync()
        self.assertEqual(result["status"], "done", result)
        return result

    def add_tracks(self):
        self.spotify_tracks = [
            spotify_track(0, "id-alpha", "Track Alpha", "Artist A", 0),
            spotify_track(0, "id-bravo", "Track Bravo", "Artist B", 1),
            spotify_track(0, "id-charlie", "Track Charlie", "Artist C", 2),
            spotify_track(0, "id-delta", "Track Delta", "Artist D", 3),
            spotify_track(0, "id-echo", "Track Echo", "Artist E", 4),
        ]
        n = self.nico
        n.offer("Artist A", "Track Alpha", "flac24_96", "u1", r"m\01 - Track Alpha.flac", {"4": 96000, "5": 24})
        n.offer("Artist B", "Track Bravo", "wav16", "u2", r"m\02 - Track Bravo.wav", {})          # WAV, no attributes at all
        # Charlie: two WAVs (same tier). The one with a free slot is tried first, turns out to be
        # compressed ADPCM, and must be rejected after download; the other is the fallback.
        n.offer("Artist C", "Track Charlie", "adpcm", "bad", r"x\03 - Track Charlie.wav", {"0": 1411}, free=True)
        n.offer("Artist C", "Track Charlie", "wav16", "good", r"y\03 - Track Charlie.wav", {"0": 1411}, free=False)
        make_audio(self.nic / "04 - Track Delta.flac", "flac16")                                    # already downloaded earlier
        n.offer("Artist E", "Track Echo", "lossy16", "u5", r"m\05 - Track Echo.flac", {"4": 44100, "5": 16})  # lossy-looking

    def files(self, folder, ext):
        return sorted((self.music / folder).glob(f"*.{ext}"))



class SoulseekSyncTest(SyncFixture):
    def test_first_sync_produces_a_playlist_per_format(self):
        self.add_tracks()
        result = self.run_sync()

        searched = self.nico.searches
        self.assertNotIn(soulseek._build_query("Artist D", "Track Delta"), searched, "a file already on disk must not be searched for")
        self.assertEqual(searched.count(soulseek._build_query("Artist C", "Track Charlie")), 2,
                         "the compressed WAV is rejected after download, then another source is searched")

        self.assertEqual(sorted(p["name"] for p in self.rbfake.playlists.values()), ["Deep tech AIFF", "Deep tech FLAC"])
        for folder, ext in (("Deep tech FLAC", "flac"), ("Deep tech AIFF", "aiff")):
            out = self.files(folder, ext)
            self.assertEqual(len(out), 5, f"{folder}: {out}")
            for f in out:
                codec, bits, rate = audio_formats.probe(f)
                self.assertEqual((codec, bits), (audio_formats.FORMATS[ext].codec, 16), f.name)
                self.assertIn(rate, (44100, 48000), f.name)
        alpha = [f for f in self.files("Deep tech AIFF", "aiff") if "Alpha" in f.name][0]
        self.assertEqual(audio_formats.probe(alpha)[2], 48000, "96 kHz goes to 48 kHz (exact ratio), not 44.1")

        self.assertEqual(result["tracks_imported"], 10)
        self.assertEqual(len(self.rbfake.imports), 10)
        for track, path in self.rbfake.imports:
            self.assertTrue(path.endswith("." + track.file_extension), (path, track.file_extension))

        # Rekordbox is written only after every download resolved, and only after waiting on it
        kinds = [e[0] for e in self.events]
        last_enqueue = max(i for i, k in enumerate(kinds) if k == "enqueue")
        first_write = min(i for i, k in enumerate(kinds) if k in ("create_playlist", "import"))
        self.assertLess(last_enqueue, first_write)
        self.assertIn(("wait", "tracks are ready to import"), self.events)
        self.assertLess(self.events.index(("wait", "tracks are ready to import")), first_write)

        # sources are consumed or archived, never left behind or deleted
        leftovers = [p for p in self.nic.iterdir() if p.suffix.lower() in audio_formats.LOSSLESS_EXTS]
        self.assertEqual(leftovers, [])
        self.assertTrue(list((self.music / "Deep tech FLAC" / "_originals").glob("*")), "converted originals are archived")
        self.assertTrue(any("Delta" in f.name for f in self.files("Deep tech FLAC", "flac")), "the matching FLAC was moved, not copied")

        # tags: Spotify's, not whatever the file came with
        tags = mutagen.File(str(alpha)).tags
        self.assertEqual((str(tags["TIT2"]), str(tags["TALB"]), str(tags["TDRC"])), ("Track Alpha", "Track Alpha EP", "2020"))

        self.assertTrue(any("Suspect file" in e and "Echo" in e for e in result["errors"]), "lossy-looking file is flagged")
        self.assertEqual(sum(1 for e in result["errors"] if "Suspect" in e), 1, "and only that one")

        for pid, titles in self.rbfake.reorders:
            self.assertEqual(titles, [t.title for t in self.spotify_tracks], "renumbered from Spotify order")
        self.assertGreaterEqual(self.rbfake.flushes, 1)

        state = self.orchestrator()._state["playlists"]["PL1"]["variants"]
        self.assertEqual({k: len(v["tracks"]) for k, v in state.items()}, {"flac": 5, "aiff": 5})

    def test_resync_with_nothing_new_does_nothing(self):
        self.add_tracks()
        self.run_sync()
        self.events.clear(); before = len(self.nico.searches); imports = len(self.rbfake.imports)
        self.run_sync()
        self.assertEqual(len(self.nico.searches), before)
        self.assertEqual(len(self.rbfake.imports), imports)
        self.assertNotIn(("wait", "tracks are ready to import"), self.events, "no writes, so nothing to wait for")

    def test_adding_a_format_later_derives_it_without_downloading(self):
        self.add_tracks()
        self.run_sync(["flac", "aiff"])
        flac_before = {f.name: f.stat().st_mtime_ns for f in self.files("Deep tech FLAC", "flac")}
        searches = len(self.nico.searches)

        self.run_sync(["flac", "aiff", "wav"])
        self.assertEqual(len(self.nico.searches), searches, "no Soulseek search: WAVs come from files we already have")
        self.assertIn("Deep tech WAV", [p["name"] for p in self.rbfake.playlists.values()])
        wavs = self.files("Deep tech WAV", "wav")
        self.assertEqual(len(wavs), 5)
        # a 16-bit/44.1 source converts bit-exactly
        delta_wav = [w for w in wavs if "Delta" in w.name][0]
        delta_flac = [f for f in self.files("Deep tech FLAC", "flac") if "Delta" in f.name][0]
        self.assertEqual(pcm_md5(delta_wav), pcm_md5(delta_flac))
        self.assertEqual({f.name: f.stat().st_mtime_ns for f in self.files("Deep tech FLAC", "flac")}, flac_before,
                         "the existing FLAC files were left untouched")

    def test_new_spotify_track_is_added_to_every_selected_format(self):
        self.add_tracks()
        self.run_sync(["flac", "aiff", "wav"])
        self.spotify_tracks.append(spotify_track(0, "id-foxtrot", "Track Foxtrot", "Artist F", 5))
        self.nico.offer("Artist F", "Track Foxtrot", "flac16", "u6", r"m\06 - Track Foxtrot.flac", {"4": 44100, "5": 16})
        self.rbfake.imports.clear()
        self.run_sync()
        self.assertEqual(sorted((t.title, t.file_extension) for t, _ in self.rbfake.imports),
                         [("Track Foxtrot", "aiff"), ("Track Foxtrot", "flac"), ("Track Foxtrot", "wav")])
        self.assertEqual(self.nico.searches.count(soulseek._build_query("Artist F", "Track Foxtrot")), 1)
        self.assertEqual(self.rbfake.reorders[-1][1][-1], "Track Foxtrot")

    def test_removed_track_leaves_the_playlists_but_not_the_disk(self):
        self.add_tracks()
        self.run_sync(["flac", "aiff"])
        self.spotify_tracks = [t for t in self.spotify_tracks if t.title != "Track Bravo"]
        self.run_sync()
        self.assertEqual(sorted(name for name, _ in self.rbfake.removed), ["Deep tech AIFF", "Deep tech FLAC"])
        self.assertTrue(any("Bravo" in f.name for f in self.files("Deep tech FLAC", "flac")), "file kept on disk")
        variants = self.orchestrator()._state["playlists"]["PL1"]["variants"]
        self.assertNotIn("id-bravo", variants["flac"]["tracks"])

    def test_old_single_format_state_is_migrated(self):
        (self.tmp / "sync_state.json").write_text(
            '{"playlists": {"PL1": {"tracks": {}, "flac_variant": {"tracks": {"id-x": {"title": "X"}}, "rb_playlist_id": "9", "display_name": "Deep tech FLAC"}}}}',
            encoding="utf-8")
        pl = self.orchestrator()._state["playlists"]["PL1"]
        self.assertNotIn("flac_variant", pl)
        self.assertEqual(pl["variants"]["flac"]["rb_playlist_id"], "9")

    def test_a_playlist_made_by_hand_is_reused_not_duplicated(self):
        """'Deep Tech AIFF' built manually must be adopted for Spotify's 'Deep tech' --
        different capitalisation, and not in the sync state at all."""
        self.add_tracks()
        made = {}
        for t in ("Track Alpha", "Track Bravo"):
            wav = self.tmp / "manual" / f"{t}.wav"
            make_audio(wav, "wav16")
            p = wav.with_suffix(".aiff")
            self.assertTrue(audio_formats.convert(wav, p, "aiff"))
            made[t] = str(p).replace("\\", "/")
        pid = self.rbfake.add_existing_playlist("Deep Tech AIFF", made)
        app_config.set_output_formats(["aiff"])
        self.run_sync()

        self.assertEqual([p["name"] for p in self.rbfake.playlists.values()], ["Deep Tech AIFF"], "no second playlist")
        imported = sorted(t.title for t, _ in self.rbfake.imports)
        self.assertEqual(imported, ["Track Charlie", "Track Delta", "Track Echo"], "the two it already had are skipped")
        self.assertNotIn(soulseek._build_query("Artist A", "Track Alpha"), self.nico.searches)
        state = self.orchestrator()._state["playlists"]["PL1"]["variants"]["aiff"]
        self.assertEqual(state["rb_playlist_id"], pid)
        self.assertEqual(len(state["tracks"]), 5)
        self.assertEqual(state["tracks"]["id-alpha"]["file_path"], made["Track Alpha"], "recorded from THAT playlist's own file")


class NoDuplicateDownloadsTest(SyncFixture):
    """The same guarantee the YouTube path gives: a track we already have -- in another
    playlist, in the Rekordbox library, or on disk -- is never downloaded again."""

    def setUp(self):
        super().setUp()
        app_config.set_output_formats(["aiff"])
        self.spotify_tracks = [spotify_track(0, "id-x", "Track Xray", "Artist X", 0)]

    def query(self):
        return soulseek._build_query("Artist X", "Track Xray")

    def test_same_track_in_two_playlists_is_downloaded_once_and_the_file_shared(self):
        self.playlist_names = {"PL1": "Deep tech", "PL2": "Classic"}
        self.nico.offer("Artist X", "Track Xray", "flac16", "u1", "m/01 - Track Xray.flac", {"4": 44100, "5": 16})
        self.run_sync()
        self.assertEqual(self.nico.searches.count(self.query()), 1, "second playlist must not search again")
        paths = [p for _, p in self.rbfake.imports]
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], paths[1], "one physical file, in both playlists")
        self.assertEqual(len(list(self.music.rglob("*.aiff"))), 1, "no second copy on disk")
        self.assertEqual(sorted(p["name"] for p in self.rbfake.playlists.values()), ["Classic AIFF", "Deep tech AIFF"])

    def test_lossless_file_already_in_the_rekordbox_library_is_reused(self):
        lib = self.tmp / "Library" / "Artist X - Track Xray.flac"
        make_audio(lib, "flac16")
        self.rbfake.add_existing_playlist("Some other playlist", {"Track Xray": str(lib).replace("\\", "/")})
        self.run_sync()
        self.assertEqual(self.nico.searches, [], "found in the library, so no Soulseek search")
        aiffs = list(self.music.rglob("*.aiff"))
        self.assertEqual(len(aiffs), 1)
        self.assertEqual(pcm_md5(aiffs[0]), pcm_md5(lib), "derived bit-exactly from the library file")
        self.assertTrue(lib.exists(), "the library file is left where it was")

    def test_an_mp3_in_the_library_is_not_a_source_for_a_lossless_playlist(self):
        lib = self.tmp / "Library" / "Artist X - Track Xray.mp3"
        lib.parent.mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=2", "-b:a", "320k", str(lib)],
                       capture_output=True, check=True)
        self.rbfake.add_existing_playlist("MP3 playlist", {"Track Xray": str(lib).replace("\\", "/")})
        self.nico.offer("Artist X", "Track Xray", "flac16", "u1", "m/01 - Track Xray.flac", {"4": 44100, "5": 16})
        self.run_sync()
        self.assertEqual(self.nico.searches.count(self.query()), 1, "an MP3 can't stand in for lossless, so it searched")
        self.assertEqual(len(list(self.music.rglob("*.aiff"))), 1)

    def test_a_full_playlist_in_one_format_becomes_another_without_downloading(self):
        """The 'I already have it all as FLAC, now I want AIFF and WAV' case: a hand-made
        FLAC playlist, sff has no record of it, and the filenames don't contain the artist."""
        flac = self.tmp / "Hand made" / "01 - Xray.flac"
        make_audio(flac, "flac16")
        self.rbfake.add_existing_playlist("Deep tech FLAC", {"Track Xray": str(flac).replace("\\", "/")})
        app_config.set_output_formats(["aiff", "wav"])
        self.run_sync()
        self.assertEqual(self.nico.searches, [], "converted from the FLAC playlist, nothing downloaded")
        for ext in ("aiff", "wav"):
            out = list(self.music.rglob(f"*.{ext}"))
            self.assertEqual(len(out), 1, ext)
            self.assertEqual(pcm_md5(out[0]), pcm_md5(flac), f"{ext} carries exactly the FLAC's audio")
        self.assertTrue(flac.exists(), "the FLAC is left alone")


class BadPeerTest(SyncFixture):
    """A peer that stalls on several tracks is deprioritised for every track."""

    def setUp(self):
        super().setUp()
        soulseek._peer_strikes.clear()
        soulseek._peer_delivered.clear()
        self.addCleanup(soulseek._peer_strikes.clear)
        self.addCleanup(soulseek._peer_delivered.clear)
        self.nico.offer("Artist Q", "Track Q", "flac16", "flaky", "a/01 - Track Q.flac", {"4": 44100, "5": 16}, free=True, speed=9000)
        self.nico.offer("Artist Q", "Track Q", "wav16", "steady", "b/01 - Track Q.wav", {}, free=False, speed=10)

    def best(self):
        best, _ = soulseek.find_candidate("Artist Q", "Track Q", "lossless", set(), soulseek._avoided_peers())
        return best["username"]

    def test_a_peer_that_keeps_failing_is_ranked_below_working_ones(self):
        self.assertEqual(self.best(), "flaky", "before any failures the better source wins")
        soulseek._peer_strikes["flaky"] = soulseek.PEER_STRIKE_LIMIT
        self.assertEqual(self.best(), "steady", "a working WAV beats a FLAC from a peer that keeps stalling")

    def test_a_peer_that_has_delivered_is_never_penalised(self):
        soulseek._peer_strikes["flaky"] = 10
        soulseek._peer_delivered.add("flaky")
        self.assertEqual(self.best(), "flaky")

    def test_a_failing_peer_is_still_used_when_it_is_the_only_source(self):
        self.nico.catalog.clear()
        self.nico.offer("Artist Q", "Track Q", "flac16", "flaky", "a/01 - Track Q.flac", {"4": 44100, "5": 16})
        soulseek._peer_strikes["flaky"] = 99
        self.assertEqual(self.best(), "flaky", "slow beats nothing")


class GiveUpEarlyTest(SyncFixture):
    def test_a_song_that_never_shows_up_is_abandoned_after_four_searches(self):
        self.spotify_tracks = [spotify_track(0, "id-ghost", "Ghost Song", "Nobody", 0)]
        app_config.set_output_formats(["aiff"])
        result = self.run_sync()
        query = soulseek._build_query("Nobody", "Ghost Song")
        self.assertEqual(self.nico.searches.count(query), 4, "1 initial + 3 retries, then it gives up")
        self.assertTrue(any("no results" in e for e in result["errors"]), result["errors"])


class ConnectionGuardTest(SyncFixture):
    """A Soulseek ban drops Nicotine+'s connection and every queued download reads
    'User logged off'. That must pause the sync, not burn each track's retries."""

    def test_waits_while_disconnected_instead_of_counting_it_as_a_dead_source(self):
        states = iter([False, False, True])
        polls = []
        mock.patch.object(soulseek, "is_connected", lambda: (polls.append(1), next(states))[1]).start()
        messages = []
        self.assertTrue(soulseek.wait_until_connected(messages.append, max_wait=3600, poll=0))
        self.assertEqual(len(polls), 3, "polled until it came back")
        self.assertEqual(len(messages), 2, "and said so while waiting")

    def test_gives_up_waiting_eventually(self):
        mock.patch.object(soulseek, "is_connected", lambda: False).start()
        self.assertFalse(soulseek.wait_until_connected(None, max_wait=0, poll=0))

    def test_searches_are_spaced_out_not_fired_together(self):
        sleeps = []
        mock.patch("time.sleep", lambda s: sleeps.append(s)).start()
        for q in ("a one", "b two", "c three"):
            self.nico.catalog.setdefault(q, [])
        soulseek._search_many(["a one", "b two", "c three"])
        self.assertEqual(sleeps.count(soulseek.SEARCH_GAP_SECONDS), 2, "a gap between searches, none before the first")


class AlreadyFailedInNicotineTest(SyncFixture):
    """Queueing a file Nicotine+ already has a record of is a no-op ('duplicate'), so a
    source that earlier ended 'File not shared' must never be picked again."""

    def setUp(self):
        super().setUp()
        self.nico.offer("Artist Q", "Track Q", "flac16", "deadguy", "a/01 - Track Q.flac", {"4": 44100, "5": 16}, free=True, speed=9000)
        self.nico.offer("Artist Q", "Track Q", "flac16", "goodguy", "b/01 - Track Q.flac", {"4": 44100, "5": 16}, free=False, speed=10)

    def pick(self):
        best, stats = soulseek.find_candidate("Artist Q", "Track Q", "lossless", set())
        return best["username"], stats

    def record(self, user, status):
        self.nico.downloads.append({"username": user, "virtual_path": ("a" if user == "deadguy" else "b") + "/01 - Track Q.flac",
                                    "status": status, "progress_pct": 0})

    def test_a_source_that_already_failed_in_nicotine_is_skipped(self):
        self.assertEqual(self.pick()[0], "deadguy", "the better source, before it is known to be dead")
        self.record("deadguy", "File not shared.")
        user, stats = self.pick()
        self.assertEqual(user, "goodguy")
        self.assertEqual(stats.blocked, 1)

    def test_a_source_that_was_already_downloaded_is_skipped_too(self):
        self.record("deadguy", "Finished")
        self.assertEqual(self.pick()[0], "goodguy")

    def test_a_user_who_is_merely_offline_is_not_blocked(self):
        """Nicotine+ resumes 'User logged off' transfers by itself when the user returns."""
        self.record("deadguy", "User logged off")
        self.assertEqual(self.pick()[0], "deadguy")

    def test_the_reason_for_giving_up_mentions_it(self):
        self.nico.catalog.clear()
        self.nico.offer("Artist Q", "Track Q", "flac16", "deadguy", "a/01 - Track Q.flac", {"4": 44100, "5": 16})
        self.record("deadguy", "File not shared.")
        best, stats = soulseek.find_candidate("Artist Q", "Track Q", "lossless", set())
        self.assertIsNone(best)
        st = soulseek._TrackState(None)
        st.note_search(stats)
        self.assertIn("already failed", st.why_no_source())


class QueryBuildingTest(unittest.TestCase):
    """Real Spotify titles that produced 0 results because of what was in the query."""

    def q(self, artist, title):
        return soulseek._build_query(artist, title)

    def test_credits_and_version_tags_are_left_out_of_the_query(self):
        self.assertEqual(self.q("Protoje, Zion I Kings, Original Koffee", "Switch Up (feat. Original Koffee) - Dub"), "Protoje Switch Up")
        self.assertEqual(self.q("Green Lion Crew, Lee \"Scratch\" Perry", "Green Brain (with Lee \"Scratch\" Perry & Yaadcore)"),
                         "Green Lion Crew Green Brain")
        self.assertEqual(self.q("Boostive, Racquel Jones", "Ties Unwind (NOiSEMAKER dub Mix)"), "Boostive Ties Unwind")
        self.assertEqual(self.q("Hotsteppas, ickle", "Standing Firm (ickle's Dub Mix)"), "Hotsteppas Standing Firm")

    def test_plain_titles_are_unchanged(self):
        self.assertEqual(self.q("Bicep", "Satisfy"), "Bicep Satisfy")
        self.assertEqual(self.q("Mungo's Hi Fi", "Pulsating Dub"), "Mungo's Hi Fi Pulsating Dub")
        self.assertEqual(self.q("Stick Figure", "Smokin' Love (with Collie Buddz) - Prince Fatty Dub"), "Stick Figure Smokin' Love")

    def test_a_title_that_is_only_a_bracket_still_produces_a_query(self):
        self.assertTrue(self.q("Artist", "(Untitled)").strip())

    def test_the_version_is_still_enforced_on_the_results(self):
        """Dropping '(NOiSEMAKER dub Mix)' from the query must not let the plain track through."""
        title = "Ties Unwind (NOiSEMAKER dub Mix)"
        self.assertTrue(soulseek.passes_version_guard(r"x\boostive - ties unwind (noisemaker dub mix).flac", title))
        self.assertFalse(soulseek.passes_version_guard(r"x\boostive - ties unwind (some other remix).flac", title))


class VersionGuardTest(unittest.TestCase):
    """Real cases from the Dub Reggae playlist, where the version IS the point."""

    g = staticmethod(soulseek.passes_version_guard)

    def test_a_named_dub_mix_needs_its_named_remixer(self):
        t = "Standing Firm (ickle's Dub Mix)"
        self.assertTrue(self.g(r"x\hotsteppas - standing firm (ickle's dub mix).flac", t))
        self.assertFalse(self.g(r"x\dub reggae\hotsteppas - standing firm (ft. donovan kingjay).wav", t),
                         "the plain track, even in a folder called 'dub'")

    def test_every_distinctive_word_of_the_remixer_is_required(self):
        t = "Green Brain (with Lee Scratch Perry) - Subatomic Sound System Remix"
        self.assertTrue(self.g(r"x\green brain (subatomic sound system remix).flac", t))
        self.assertFalse(self.g(r"x\some sound system\green brain.flac", t))

    def test_original_mix_is_not_the_remix(self):
        t = "Smokin' Love - Prince Fatty Dub"
        self.assertFalse(self.g(r"x\stick figure - smokin' love (original mix).flac", t))
        self.assertTrue(self.g(r"x\09 - smokin' love (prince fatty dub).flac", t))

    def test_a_plain_dub_tag_needs_dub_in_the_path(self):
        self.assertTrue(self.g(r"x\01 - switch up (dub).flac", "Switch Up - Dub"))
        self.assertFalse(self.g(r"x\01 - switch up.flac", "Switch Up - Dub"))

    def test_plain_titles_are_unchanged(self):
        self.assertTrue(self.g(r"x\bicep - satisfy (original mix).flac", "Satisfy"))
        self.assertFalse(self.g(r"x\bicep - satisfy (some remix).flac", "Satisfy"))


class ApiRetryTest(unittest.TestCase):
    """A 504 from Nicotine+'s API once ended a multi-hour sync."""

    class _Resp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self.body

    def http_error(self, code):
        import urllib.error
        return urllib.error.HTTPError("http://x", code, "err", {}, None)

    def run_with(self, outcomes):
        calls = []
        def fake_urlopen(*a, **k):
            calls.append(1)
            o = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(o, Exception):
                raise o
            return self._Resp(o)
        sleeps = []
        with mock.patch("urllib.request.urlopen", fake_urlopen), mock.patch("time.sleep", lambda s: sleeps.append(s)):
            try:
                result = soulseek._api_get("/x")
            except Exception as e:
                result = e
        return result, len(calls), sleeps

    def test_a_temporary_504_is_retried_until_it_works(self):
        result, calls, sleeps = self.run_with([self.http_error(504), self.http_error(504), b'{"ok": true}'])
        self.assertEqual(result, {"ok": True})
        self.assertEqual((calls, sleeps), (3, [3, 10]))

    def test_a_dropped_connection_is_retried_too(self):
        import urllib.error
        result, calls, _ = self.run_with([urllib.error.URLError("connection reset"), b'{"ok": 1}'])
        self.assertEqual((result, calls), ({"ok": 1}, 2))

    def test_it_gives_up_after_the_last_delay(self):
        result, calls, sleeps = self.run_with([self.http_error(504)])
        self.assertIsInstance(result, Exception)
        self.assertEqual(calls, len(soulseek.API_RETRY_DELAYS) + 1)

    def test_a_real_error_is_not_retried(self):
        result, calls, sleeps = self.run_with([self.http_error(400)])
        self.assertIsInstance(result, Exception)
        self.assertEqual((calls, sleeps), (1, []))


class FormatHierarchyTest(SyncFixture):
    """FLAC first, then WAV and AIFF as equals, then a 320 MP3 as the last resort."""

    def best(self, title="Track Q", artist="Artist Q", mode="lossless", exclude=()):
        best, _ = soulseek.find_candidate(artist, title, mode, set(exclude))
        return best and best["username"]

    def test_flac_beats_wav_and_aiff_even_without_a_free_slot(self):
        self.nico.offer("Artist Q", "Track Q", "wav16", "wavguy", r"a - Track Q.wav", {"0": 1411}, free=True, speed=9000)
        self.nico.offer("Artist Q", "Track Q", "flac16", "flacguy", r"b - Track Q.flac", {"4": 44100, "5": 16}, free=False, speed=1)
        self.nico.offer("Artist Q", "Track Q", "wav16", "aiffguy", r"c - Track Q.aiff", {"0": 1411}, free=True, speed=9000)
        self.assertEqual(self.best(), "flacguy")

    def test_wav_and_aiff_are_equals(self):
        # identical evidence -> the free slot decides, not the container
        self.nico.offer("Artist Q", "Track Q", "wav16", "aiffguy", r"a - Track Q.aiff", {}, free=False)
        self.nico.offer("Artist Q", "Track Q", "wav16", "wavguy", r"b - Track Q.wav", {}, free=True)
        self.assertEqual(self.best(), "wavguy")
        # ...and the other way round
        self.nico.catalog.clear()
        self.nico.offer("Artist Q", "Track Q", "wav16", "wavguy", r"a - Track Q.wav", {}, free=False)
        self.nico.offer("Artist Q", "Track Q", "wav16", "aiffguy", r"b - Track Q.aiff", {}, free=True)
        self.assertEqual(self.best(), "aiffguy")

    def test_lossless_beats_mp3_and_mp3_is_only_a_fallback_mode(self):
        self.nico.offer("Artist Q", "Track Q", "flac16", "mp3guy", r"a - Track Q.mp3", {"0": 320}, free=True)
        self.assertIsNone(self.best(), "an MP3 is never picked while looking for lossless")
        self.assertEqual(self.best(mode="mp3"), "mp3guy")
        self.nico.offer("Artist Q", "Track Q", "wav16", "lateguy", r"c\01 - Track Q.wav", {}, free=False)
        self.assertEqual(self.best(mode="mp3"), "lateguy", "the fallback stage still prefers lossless if it turns up")
        self.nico.catalog.clear()
        self.nico.offer("Artist Q", "Track Q", "flac16", "mp3guy", r"a\01 - Track Q.mp3", {"0": 320}, free=True)
        self.nico.offer("Artist Q", "Track Q", "wav16", "wavguy", r"b - Track Q.wav", {}, free=False)
        self.assertEqual(self.best(), "wavguy", "even a slot-less WAV beats an MP3")

    def test_local_files_follow_the_same_hierarchy(self):
        make_audio(self.nic / "01 - Track Q.wav", "wav16")
        make_audio(self.nic / "01 - Track Q.flac", "flac16")
        (self.nic / "01 - Track Q.wav").write_bytes((self.nic / "01 - Track Q.wav").read_bytes() * 4)   # bigger, but WAV
        track = spotify_track(0, "id-q", "Track Q", "Artist Q", 0)
        states = soulseek.search_and_queue_all([track], local_dir=str(self.nic), prefer_exts={".wav"})
        self.assertEqual(states["id-q"].local_path.suffix, ".flac", "FLAC outranks a bigger WAV, even when WAV is the wanted container")


class MatcherTest(unittest.TestCase):
    """Real filenames from Soulseek that the matcher used to get wrong."""

    def match(self, path, artist, title):
        return soulseek.is_exact_title_match(path, artist, title) and soulseek.passes_version_guard(path.lower(), title)

    def test_title_sharing_a_word_with_the_artist_name(self):
        a, t = "Barry Can't Swim, Laurence Guy", "Can We Still Be Friends?"
        self.assertTrue(self.match(r"x\Barry Can't Swim - Can We Still Be Friends-.aiff", a, t))
        self.assertTrue(self.match(r"x-barry_cant_swim-can_we_still_be_friends_(with_laurence_guy)_(original_mix).flac", a, t))
        self.assertTrue(self.match("x\Barry Can’t Swim - More Content - 02 - Can We Still Be Friends.mp3", a, t))
        self.assertFalse(self.match(r"x\Barry Can't Swim - God Is The Space Between Us.flac", a, t))
        self.assertFalse(self.match(r"x\Barry Can't Swim - Can We Still Be Friends (Someone Remix).flac", a, t))

    def test_one_word_vs_two_word_spelling(self):
        self.assertTrue(self.match(r"x\Modjo - Roller Coaster.mp3", "Modjo", "Rollercoaster"))
        self.assertFalse(self.match(r"x\Modjo - Roller Skate.mp3", "Modjo", "Rollercoaster"))

    def test_prefix_fallback_query_only_for_single_long_words(self):
        self.assertEqual(soulseek._fallback_query("Modjo", "Rollercoaster"), "Modjo Roller")
        self.assertIsNone(soulseek._fallback_query("Bicep", "Opal"))
        self.assertIsNone(soulseek._fallback_query("Bicep", "Two Words Here"))

    def test_fallback_search_finds_the_two_word_spelling(self):
        events = []
        nico = FakeNicotine(Path(tempfile.mkdtemp()), events)
        nico.catalog[soulseek._fallback_query("Modjo", "Rollercoaster")] = [{
            "username": "u1", "file_path": r"m\Modjo - Roller Coaster.wav", "size": 1000, "file_attributes": {},
            "free_upload_slots": True, "upload_speed": 1000, "_kind": "wav16"}]
        with mock.patch.object(soulseek, "_api_post", nico.api_post),              mock.patch.object(soulseek, "_fetch_all_results", nico.fetch_all_results),              mock.patch.object(soulseek, "SEARCH_WAIT_SECONDS", 0):
            best, _ = soulseek.find_candidate("Modjo", "Rollercoaster", "lossless", set())
        self.assertEqual(best["username"], "u1")


class NumberedTitleTest(unittest.TestCase):
    """Real mistakes from a sync: 'Liquid Interlude 2' was matched by the files for
    'Liquid Interlude 1' and '4' (digits were being ignored), and 'Wake Up - Mix 1'
    by '(Mix 2)'. A wrong-numbered file would have been imported under the right name."""

    def match(self, path, artist, title):
        main = re.split(r"\s+-\s+", title, maxsplit=1)[0]        # as find_candidate does
        return soulseek.is_exact_title_match(path, artist, main) and soulseek.passes_version_guard(path.lower(), title)

    def test_numbered_titles_only_match_their_own_number(self):
        a, t = "Kings Of Tomorrow", "Liquid Interlude 2"
        self.assertTrue(self.match(r"x\02 - Kings of Tomorrow - Liquid Interlude 2.flac", a, t))
        self.assertTrue(self.match(r"x\02_-_Kings_Of_Tomorrow_-_Liquid_Interlude_2.flac", a, t))
        self.assertFalse(self.match(r"x\01 - Kings of Tomorrow - Liquid Interlude 1.flac", a, t))
        self.assertFalse(self.match(r"x\04_-_Kings_Of_Tomorrow_-_Liquid_Interlude_4.flac", a, t))

    def test_numbered_mix_only_matches_its_own_number(self):
        a, t = "Fire Island", "Wake Up - Mix 1"
        self.assertTrue(self.match(r"x\Fire Island - Wake Up (Mix 1).flac", a, t))
        self.assertFalse(self.match(r"x\Fire Island - Wake Up (Mix 2).flac", a, t))
        self.assertFalse(self.match(r"x\Fire Island - Wake Up (Mix 2).flac", "Fire Island", "Wake Up"))

    def test_compilation_folder_numbers_do_not_count(self):
        self.assertTrue(self.match(r"x\Ten Years of On Loop, Vol. 1\K-Lone - iluvu.flac", "K-Lone", "iluvu"))

    def test_title_with_a_digit_still_matches_its_own_filenames(self):
        a, t = "Mount Kimbi", "No Need 2 Be Sorry, Call Me?"
        self.assertTrue(self.match(r"x\02 No Need 2 Be Sorry, Call Me_.flac", a, t))
        self.assertTrue(self.match(r"x\02-mount_kimbi-no_need_2_be_sorry_call_me.flac", a, t))


class FilenameConventionTest(unittest.TestCase):
    """The matcher must accept the SAME song however people name the file. This is the
    net that would have caught 'Can We Still Be Friends' by 'Barry Can't Swim' (a title
    sharing a word with the artist) before it silently failed in a real sync."""

    PAIRS = [
        ("Bicep", "Satisfy"),
        ("Barry Can't Swim, Laurence Guy", "Can We Still Be Friends?"),
        ("Four Tet, KH, Nelly Furtado", "Only Human"),
        ("Fatima Yamaha", "What's a Girl to Do"),
        ("Ross from Friends", "Epiphany"),
        ("Prince Fatty, Little Roy", "Roof Over My Dub"),
        ("Scientist, Hempress Sativa", "Rock It Ina Dub"),
        ("Modjo", "Rollercoaster"),
        ("Overmono", "Is U"),
        ("Mount Kimbi", "No Need 2 Be Sorry, Call Me?"),
        ("Dense & Pika", "Colt"),
        ("Four Tet", "Four Tet Loves You"),          # title contains the whole artist name
        ("Soul Wun", "Blue Light"),
    ]

    @staticmethod
    def conventions(artist, title):
        first = artist.split(",")[0].strip()
        safe = lambda x: re.sub(r'[?:*"<>|/]', "_", x)
        snake = lambda x: re.sub(r"[^a-z0-9()]+", "_", x.lower().replace("'", "").replace("\u2019", "")).strip("_")
        yield f"{first} - {title}.flac"
        yield f"{safe(first)} - {safe(title)}.flac"
        yield f"01 - {safe(title)}.flac"
        yield f"01. {first} - {safe(title)}.wav"
        yield f"07 {safe(title)} (Original Mix).aiff"
        yield f"{first} - {safe(title)} (Original Mix).flac"
        yield f"01-{snake(first)}-{snake(title)}_(original_mix).flac"
        yield f"{first.replace(chr(39), chr(0x2019))} - {safe(title)}.wav"
        yield f"D1 A2 {safe(title)} ({first}).flac"

    def test_every_naming_convention_matches_its_own_song(self):
        misses = []
        for artist, title in self.PAIRS:
            for name in self.conventions(artist, title):
                if not soulseek.is_exact_title_match("share\\" + name, artist, title):
                    misses.append(f"{artist} - {title}: {name}")
        self.assertEqual(misses, [], "filenames of the right song that the matcher rejected")

    def test_a_different_song_by_the_same_artist_never_matches(self):
        for artist, title in self.PAIRS:
            first = artist.split(",")[0].strip()
            for other in ("Some Other Song", "Totally Different"):
                self.assertFalse(soulseek.is_exact_title_match(f"share\{first} - {other}.flac", artist, title),
                                 f"{artist} - {other} matched {title}")


if __name__ == "__main__":
    unittest.main()
