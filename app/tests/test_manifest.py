"""Moving the library to another computer: the manifest and its importer."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import manifest  # noqa: E402


class FakeRb:
    """Enough of services.rekordbox for the exporter and the importer."""

    def __init__(self, playlists=None):
        self.playlists = playlists or {}                  # name -> list of entry dicts (export side)
        self.ids, self.contents, self.members, self.n = {}, {}, {}, 100
        self.flushed = False

    # export side
    def find_playlist_id(self, name):
        return f"pl-{name}" if name in self.playlists else None

    def get_playlist_entries(self, pid):
        return list(self.playlists[pid[3:]])

    # import side
    def find_or_create_playlist(self, name):
        return self.ids.setdefault(name, f"id-{len(self.ids) + 1}")

    def import_track_unanalyzed(self, path, track):
        cid = self.contents.setdefault(path, {"id": f"c{len(self.contents) + 1}", "title": track.title, "artist": track.artist,
                                              "album": track.album, "year": track.year})["id"]
        return {"id": cid}

    def add_track_to_playlist(self, pid, cid, no):
        self.members.setdefault(pid, {})[cid] = no
        return True

    def flush_wal(self):
        self.flushed = True


def entry(title, path, artist="Artist", album="Album", year="2020"):
    return {"title": title, "path": path, "artist": artist, "album": album, "year": year, "track_no": 0}


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.root = "D:/Music/Incoming"
        self.state = {"playlists": {"SPOT1": {"name": "FF Dub", "display_name": "Dub", "variants": {"aiff": {
            "display_name": "Dub AIFF", "rb_playlist_id": "9", "tracks": {
                "sid-a": {"file_path": f"{self.root}/Dub AIFF/a.aiff", "title": "A", "artist": "X",
                          "source": {"user": "peer", "remote_path": "r/a.flac"}},
                "sid-b": {"file_path": f"{self.root}/Dub AIFF/b.aiff", "title": "B", "artist": "X",
                          "stand_in": True, "stand_in_label": "Dub Mix"}}}}}}}
        self.rb = FakeRb({"Dub AIFF": [
            entry("B [Dub Mix]", f"{self.root}/Dub AIFF/b.aiff"),
            entry("A", f"{self.root}/Dub AIFF/a.aiff"),
            entry("Kept Different Song", f"{self.root}/Dub AIFF/kept.aiff", artist="Somebody Else"),
            entry("Old YouTube MP3", "D:/Music/Import/old.mp3"),
        ]})

    def build(self):
        self.state["playlists"]["SPOT1"]["variants"]["aiff"]["rb_playlist_id"] = "pl-Dub AIFF"
        return manifest.build_manifest(self.state, self.root, self.rb)

    def test_the_manifest_keeps_order_titles_labels_and_stand_ins(self):
        m = self.build()
        (pl,) = m["playlists"]
        self.assertEqual([t["title"] for t in pl["tracks"]], ["B [Dub Mix]", "A", "Kept Different Song"])
        self.assertEqual([t["position"] for t in pl["tracks"]], [1, 2, 3])
        b, a, kept = pl["tracks"]
        self.assertEqual((b["spotify_id"], b["stand_in"], b["stand_in_label"]), ("sid-b", True, "Dub Mix"))
        self.assertEqual((a["spotify_id"], a["stand_in"], a["source"]["user"]), ("sid-a", False, "peer"))
        self.assertEqual((kept["spotify_id"], kept["artist"]), (None, "Somebody Else"), "a kept song is not tied to Spotify")
        self.assertEqual(a["path"], "Dub AIFF/a.aiff", "relative to the music folder, so it works anywhere")

    def test_a_file_outside_the_music_folder_is_reported_not_silently_lost(self):
        m = self.build()
        self.assertEqual([o["title"] for o in m["not_exported_outside_music_root"]], ["Old YouTube MP3"])

    def test_the_manifest_is_plain_json_that_survives_a_round_trip(self):
        m = self.build()
        self.assertEqual(json.loads(json.dumps(m, ensure_ascii=False)), m)


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mac_root = self.tmp / "Volumes" / "SSD" / "Music" / "Incoming"
        (self.mac_root / "Dub AIFF").mkdir(parents=True)
        for name in ("a.aiff", "b.aiff", "kept.aiff"):
            (self.mac_root / "Dub AIFF" / name).write_bytes(b"x")
        self.manifest = {"format": 1, "playlists": [{
            "name": "Dub AIFF", "format": "aiff", "spotify_playlist_id": "SPOT1", "spotify_name": "FF Dub", "base_name": "Dub",
            "tracks": [
                {"position": 1, "title": "B [Dub Mix]", "artist": "X", "album": "Al", "year": "2020", "path": "Dub AIFF/b.aiff",
                 "spotify_id": "sid-b", "stand_in": True, "stand_in_label": "Dub Mix", "source": None},
                {"position": 2, "title": "A", "artist": "X", "album": "Al", "year": "2021", "path": "Dub AIFF/a.aiff",
                 "spotify_id": "sid-a", "stand_in": False, "stand_in_label": "", "source": {"user": "peer", "remote_path": "r/a.flac"}},
                {"position": 3, "title": "Kept Different Song", "artist": "Somebody Else", "album": "", "year": "", "path": "Dub AIFF/kept.aiff",
                 "spotify_id": None, "stand_in": False, "stand_in_label": "", "source": None},
                {"position": 4, "title": "Not Copied Over", "artist": "X", "album": "", "year": "", "path": "Dub AIFF/gone.aiff",
                 "spotify_id": "sid-z", "stand_in": False, "stand_in_label": "", "source": None}]}]}

    def test_the_plan_lists_found_and_missing_files_without_touching_anything(self):
        plan = manifest.plan_import(self.manifest, str(self.mac_root))
        self.assertEqual(len(plan["found"]), 3)
        self.assertEqual([t["title"] for _, t, _ in plan["missing"]], ["Not Copied Over"])

    def test_applying_builds_the_playlist_in_order_with_the_exact_titles_and_this_computers_paths(self):
        rb, state = FakeRb(), {}
        done = manifest.apply_import(self.manifest, str(self.mac_root), rb, state, say=lambda *_: None)
        self.assertEqual(done, {"playlists": 1, "tracks": 3, "missing": 1})
        titles = {path: c["title"] for path, c in rb.contents.items()}
        self.assertEqual(sorted(titles.values()), ["A", "B [Dub Mix]", "Kept Different Song"], "labels and kept songs exact")
        self.assertTrue(all(p.startswith((self.mac_root).as_posix()) for p in titles), "paths are the Mac's, not the PC's")
        members = rb.members["id-1"]
        order = sorted(members, key=members.get)
        self.assertEqual([next(c["title"] for c in rb.contents.values() if c["id"] == cid) for cid in order],
                         ["B [Dub Mix]", "A", "Kept Different Song"])

    def test_sffs_own_records_are_seeded_including_stand_ins_but_not_kept_songs(self):
        state = {}
        manifest.apply_import(self.manifest, str(self.mac_root), FakeRb(), state, say=lambda *_: None)
        tracks = state["playlists"]["SPOT1"]["variants"]["aiff"]["tracks"]
        self.assertEqual(sorted(tracks), ["sid-a", "sid-b"], "the kept song and the missing file have no Spotify record")
        self.assertTrue(tracks["sid-b"]["stand_in"])
        self.assertEqual(tracks["sid-b"]["stand_in_label"], "Dub Mix", "so a later sync there still looks for the real mix")
        self.assertEqual(tracks["sid-a"]["file_path"], (self.mac_root / "Dub AIFF" / "a.aiff").as_posix())
        self.assertEqual(tracks["sid-a"]["source"]["user"], "peer")
        self.assertEqual(state["playlists"]["SPOT1"]["variants"]["aiff"]["rb_playlist_id"], "id-1")

    def test_running_it_twice_does_not_duplicate_anything(self):
        rb, state = FakeRb(), {}
        for _ in range(2):
            manifest.apply_import(self.manifest, str(self.mac_root), rb, state, say=lambda *_: None)
        self.assertEqual(len(rb.contents), 3)
        self.assertEqual(len(rb.members["id-1"]), 3)

    def run_cli(self, *extra, running=False):
        path = self.tmp / "m.json"
        path.write_text(json.dumps(self.manifest), encoding="utf-8")
        rb = FakeRb()
        with mock.patch("services.platform_paths.is_rekordbox_running", return_value=running), \
             mock.patch.object(manifest, "backup_rekordbox", return_value=self.tmp / "backup") as backup, \
             mock.patch.dict(sys.modules, {"services.rekordbox": rb}), \
             mock.patch("services.rekordbox", rb, create=True):
            code = manifest.main(["import", "--manifest", str(path), "--music-root", str(self.mac_root),
                                  "--state", str(self.tmp / "state.json"), *extra])
        return code, rb, backup

    def test_without_apply_it_is_a_dry_run(self):
        code, rb, backup = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual((rb.contents, rb.ids), ({}, {}))
        backup.assert_not_called()
        self.assertFalse((self.tmp / "state.json").exists())

    def test_it_refuses_while_rekordbox_is_open(self):
        code, rb, backup = self.run_cli("--apply", running=True)
        self.assertEqual(code, 3)
        self.assertEqual(rb.contents, {})
        backup.assert_not_called()

    def test_apply_backs_up_first_then_builds_and_writes_the_state(self):
        code, rb, backup = self.run_cli("--apply")
        backup.assert_called_once()
        self.assertEqual(len(rb.contents), 3)
        self.assertTrue(rb.flushed)
        state = json.loads((self.tmp / "state.json").read_text(encoding="utf-8"))
        self.assertIn("SPOT1", state["playlists"])
        self.assertEqual(code, 1, "exit code 1 because one file was missing: nothing is hidden")

    def test_a_manifest_from_a_newer_sff_is_refused_not_misread(self):
        self.manifest["format"] = 99
        path = self.tmp / "m.json"
        path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.assertEqual(manifest.main(["import", "--manifest", str(path), "--music-root", str(self.mac_root)]), 2)


if __name__ == "__main__":
    unittest.main()
