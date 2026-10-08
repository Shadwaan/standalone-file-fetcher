import tempfile
import unittest
from pathlib import Path

from services import setlists, soulseek
from services import rekordbox as rb
from tests.test_soulseek_sync import SyncFixture


class ParsingTest(unittest.TestCase):
    def test_time_stamp_artist_title_and_mix(self):
        self.assertEqual(setlists.parse_line("0:05:45 Bizza - Metamorfosis (Brad Brunner Remix)"),
                         ("Bizza", "Metamorfosis (Brad Brunner Remix)"))
        self.assertEqual(setlists.parse_line("12:30 Rework - Loin De Moi"), ("Rework", "Loin De Moi"))
        self.assertEqual(setlists.parse_line("Rework - Loin De Moi"), ("Rework", "Loin De Moi"))

    def test_only_the_first_dash_splits_artist_from_title(self):
        self.assertEqual(setlists.parse_line("0:01:00 A - B - C"), ("A", "B - C"))

    def test_a_trailing_square_bracket_mix_becomes_round_so_it_is_not_taken_for_a_stand_in(self):
        artist, title = setlists.parse_line("0:00:30 DJ Sneak - Back & Forth (feat. K.E) [DJ Lukke 'Deep Energy Channel' Dub]")
        self.assertEqual(title, "Back & Forth (feat. K.E) (DJ Lukke 'Deep Energy Channel' Dub)")

    def test_lines_that_are_not_tracks_are_skipped(self):
        self.assertIsNone(setlists.parse_line("0:00:30 just some words"))
        self.assertIsNone(setlists.parse_line(""))

    def test_the_playlist_is_named_after_the_file(self):
        self.assertEqual(setlists.playlist_name(Path("House at 3_.txt")), "House at 3")
        self.assertEqual(setlists.playlist_name(Path("Tasting 1-2-3.txt")), "Tasting 1-2-3")

    def test_ids_are_stable_and_distinct(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "Set.txt"
            f.write_text("0:00:30 A - One\n0:03:00 B - Two\n", encoding="utf-8")
            first, again = setlists.read_tracks(f), setlists.read_tracks(f)
            self.assertEqual([t.spotify_id for t in first], [t.spotify_id for t in again])
            self.assertEqual(len({t.spotify_id for t in first}), 2)
            self.assertEqual([t.position for t in first], [0, 1])


class SetlistSyncTest(SyncFixture):
    def setUp(self):
        super().setUp()
        self.lists = self.tmp / "TrackList"
        self.lists.mkdir()
        (self.lists / "Late set_.txt").write_text(
            "0:00:30 Artist Q - Song Quebec (Club Mix)\n0:04:00 Artist R - Song Romeo\n", encoding="utf-8")
        self.nico.offer("Artist Q", "Song Quebec (Club Mix)", "flac16", "u1", r"m\01 - Song Quebec (Club Mix).flac", {"4": 44100, "5": 16})

    def run_lists(self):
        orch = self.orchestrator()
        result = orch.run_setlists(str(self.lists))
        self.assertEqual(result["status"], "done", result)
        return result

    def test_a_list_becomes_a_playlist_per_format_in_list_order(self):
        result = self.run_lists()
        names = sorted(p["name"] for p in self.rbfake.playlists.values())
        self.assertEqual(names, ["Late set AIFF", "Late set FLAC"])
        pl = next(p for p in self.rbfake.playlists.values() if p["name"] == "Late set FLAC")
        self.assertEqual([self.rbfake.contents[c]["title"] for c in pl["tracks"]][:1], ["Song Quebec (Club Mix)"])
        self.assertTrue(list((self.music / "Late set FLAC").glob("*.flac")))

    def test_a_second_run_does_not_download_again(self):
        self.run_lists()
        searches = len(self.nico.searches)
        self.run_lists()
        self.assertEqual(len(self.nico.searches), searches, "what is already in the library is not fetched twice")

    def test_a_track_nobody_has_is_reported_not_invented(self):
        result = self.run_lists()
        self.assertGreaterEqual(result["tracks_failed"], 1)


if __name__ == "__main__":
    unittest.main()
