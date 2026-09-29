"""
End-to-end test of the Soulseek sync, with Nicotine+, Spotify and Rekordbox faked
and everything else real: ffmpeg conversion, tagging, the watchdog, state handling.

Nothing here touches a real Rekordbox library, Soulseek, or your sync_state.json.
Needs ffmpeg/ffprobe on PATH.   Run:   python -m unittest discover -s tests -v
"""

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
                           ("get_status", lambda: {"running": True, "api_reachable": True})]:
            mock.patch.object(soulseek, name, attr).start()
        mock.patch("time.sleep", lambda s: None).start()
        for name in ("find_playlist_id", "get_playlist_name", "get_playlist_track_paths", "find_or_create_playlist",
                     "import_track_unanalyzed", "add_track_to_playlist", "remove_track_from_playlist",
                     "reorder_playlist_by_titles", "flush_wal"):
            mock.patch.object(rb, name, getattr(self.rbfake, name)).start()

        self.spotify_tracks = []
        outer = self

        class FakeSpotify:
            def get_prefixed_playlists(self):
                return [{"id": "PL1", "name": "FF Deep tech", "display_name": "Deep tech", "snapshot_id": "s1"}]

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
        self.nico.offer("Artist Q", "Track Q", "wav16", "wavguy", r"b - Track Q.wav", {}, free=False)
        self.assertEqual(self.best(), "wavguy", "even a slot-less WAV beats an MP3")

    def test_local_files_follow_the_same_hierarchy(self):
        make_audio(self.nic / "01 - Track Q.wav", "wav16")
        make_audio(self.nic / "01 - Track Q.flac", "flac16")
        (self.nic / "01 - Track Q.wav").write_bytes((self.nic / "01 - Track Q.wav").read_bytes() * 4)   # bigger, but WAV
        track = spotify_track(0, "id-q", "Track Q", "Artist Q", 0)
        states = soulseek.search_and_queue_all([track], local_dir=str(self.nic), prefer_exts={".wav"})
        self.assertEqual(states["id-q"].local_path.suffix, ".flac", "FLAC outranks a bigger WAV, even when WAV is the wanted container")


if __name__ == "__main__":
    unittest.main()
