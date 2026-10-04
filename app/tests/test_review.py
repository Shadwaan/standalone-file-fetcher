"""The listen-and-mark review page: its data, marks, and which files it will serve."""
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

import review  # noqa: E402


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.new_a, self.old_a = self.tmp / "new" / "a.aiff", self.tmp / "old" / "a.mp3"
        self.new_b = self.tmp / "new" / "b.aiff"
        self.new_a.parent.mkdir(parents=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "pcm_s16be",
                        str(self.new_a)], capture_output=True, check=True)
        self.old_a.parent.mkdir(parents=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-b:a", "128k",
                        str(self.old_a)], capture_output=True, check=True)
        self.new_b.write_bytes(b"x")                                  # exists, but has no YouTube copy

        audit = self.tmp / "audit.csv"
        with audit.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["playlist", "title", "verdict", "similarity_to_reference", "bass_share",
                                               "new_file", "reference"])
            w.writeheader()
            w.writerow({"playlist": "FF Oldies", "title": "Pleasure Love", "verdict": "WRONG SONG", "similarity_to_reference": "0.59",
                        "bass_share": "0.36", "new_file": str(self.new_a), "reference": str(self.old_a)})
            w.writerow({"playlist": "FF Oldies", "title": "Time After Time", "verdict": "VOCALS-ONLY / NO BASS",
                        "similarity_to_reference": "", "bass_share": "0.018", "new_file": str(self.new_b), "reference": ""})
        state = self.tmp / "state.json"
        state.write_text(json.dumps({"playlists": {"p": {"variants": {"aiff": {"tracks": {"t1": {
            "file_path": str(self.new_a).replace("\\", "/"), "artist": "Supafly & De Funk", "title": "Pleasure Love"}}}}}}}), encoding="utf-8")
        for name, value in (("AUDIT_FILE", audit), ("MARKS_FILE", self.tmp / "marks.json"), ("CACHE_DIR", self.tmp / "cache"),
                            ("STATE_FILE", state)):
            mock.patch.object(review, name, value).start()
        self.addCleanup(mock.patch.stopall)
        self.items = {i["title"]: i for i in review.load_items()}

    def test_items_carry_what_the_page_shows(self):
        pl = self.items["Pleasure Love"]
        self.assertEqual((pl["artist"], pl["group"], pl["similarity"], pl["has_new"], pl["has_old"]),
                         ("Supafly & De Funk", "grey", 0.59, True, True))
        self.assertEqual(self.items["Time After Time"]["group"], "stem")
        self.assertFalse(self.items["Time After Time"]["has_old"], "no YouTube copy to compare with")

    def test_groups_follow_the_similarity_bands(self):
        g = lambda sim, verdict="WRONG SONG": review._group({"verdict": verdict, "similarity_to_reference": sim})
        self.assertEqual([g("0.2"), g("0.4"), g("0.8"), g("")], ["wrong_clear", "grey", "version", "other"])

    def test_a_mark_is_saved_and_can_be_cleared(self):
        iid = self.items["Pleasure Love"]["id"]
        review.review_mark(review.Mark(id=iid, mark="wrong"))
        self.assertEqual({i["title"]: i["mark"] for i in review.load_items()}["Pleasure Love"], "wrong")
        self.assertEqual(review.review_items()["marked"], 1)
        review.review_mark(review.Mark(id=iid, mark=None))
        self.assertEqual(review.review_items()["marked"], 0)

    def test_an_unknown_mark_or_a_malformed_id_is_refused(self):
        with self.assertRaises(HTTPException):
            review.review_mark(review.Mark(id=self.items["Pleasure Love"]["id"], mark="both_ok"))
        with self.assertRaises(HTTPException):
            review.review_mark(review.Mark(id="../../etc/passwd", mark="both_ok"))

    def test_only_files_named_in_the_audit_can_be_served(self):
        for bad in ("deadbeef0000", "..%2F..%2Fmain", "0" * 12):
            with self.assertRaises(HTTPException) as ctx:
                review.review_audio(bad, "new")
            self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException):
            review.review_audio(self.items["Pleasure Love"]["id"], "etc")

    def test_a_missing_file_is_a_404_not_a_crash(self):
        with self.assertRaises(HTTPException) as ctx:
            review.review_audio(self.items["Time After Time"]["id"], "old")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_an_aiff_is_converted_for_the_browser_once_and_an_mp3_is_served_as_is(self):
        playable = review._playable(self.new_a)
        self.assertEqual(playable.suffix, ".mp3")
        mtime = playable.stat().st_mtime_ns
        self.assertEqual(review._playable(self.new_a), playable)
        self.assertEqual(playable.stat().st_mtime_ns, mtime, "cached, not converted again")
        self.assertEqual(review._playable(self.old_a), self.old_a)
        response = review.review_audio(self.items["Pleasure Love"]["id"], "new")
        self.assertEqual(Path(response.path), playable)


    def test_items_carry_the_spotify_id_for_the_embedded_player(self):
        self.assertEqual(self.items["Pleasure Love"]["spotify_id"], "t1")
        self.assertEqual(self.items["Time After Time"]["spotify_id"], "")

    def test_youtube_results_are_searched_by_artist_and_title_and_cached(self):
        found = [{"id": "abc123", "title": "Pleasure Love", "channel": "Supafly - Topic", "duration": 210, "url": "u"}]
        review._yt_cache.clear()
        with mock.patch.object(review, "_youtube_search", return_value=found) as search:
            first = review.review_youtube(self.items["Pleasure Love"]["id"])
            again = review.review_youtube(self.items["Pleasure Love"]["id"])
        self.assertEqual(first["results"], found)
        self.assertEqual(first["query"], "Supafly & De Funk Pleasure Love")
        search.assert_called_once()                                    # the second look did not search again
        self.assertEqual(again, first)

    def test_a_failed_youtube_search_is_a_clean_502_and_an_unknown_item_a_404(self):
        review._yt_cache.clear()
        with mock.patch.object(review, "_youtube_search", side_effect=RuntimeError("offline")):
            with self.assertRaises(HTTPException) as ctx:
                review.review_youtube(self.items["Pleasure Love"]["id"])
        self.assertEqual(ctx.exception.status_code, 502)
        with self.assertRaises(HTTPException) as ctx:
            review.review_youtube("deadbeef0000")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_the_cover_comes_from_the_new_file_and_is_a_404_when_there_is_none(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
        with mock.patch("services.tagging.embedded_picture", return_value=png):
            response = review.review_cover(self.items["Pleasure Love"]["id"])
        self.assertEqual((response.media_type, response.body), ("image/png", png))
        with mock.patch("services.tagging.embedded_picture", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                review.review_cover(self.items["Pleasure Love"]["id"])
        self.assertEqual(ctx.exception.status_code, 404)


    def test_marks_from_the_old_four_choice_page_are_converted_not_lost(self):
        review.MARKS_FILE.write_text(json.dumps({
            "a" * 12: {"mark": "youtube_right"}, "b" * 12: {"mark": "neither"},
            "c" * 12: {"mark": "new_right"}, "d" * 12: {"mark": "both_ok"}, "e" * 12: {"mark": "wrong"}}), encoding="utf-8")
        self.assertEqual({k[0]: v["mark"] for k, v in review.load_marks().items()},
                         {"a": "wrong", "b": "wrong", "c": "right", "d": "right", "e": "wrong"})
        self.assertIn("wrong", review.MARKS_FILE.read_text(encoding="utf-8"), "and the file itself is rewritten")
        self.assertNotIn("youtube_right", review.MARKS_FILE.read_text(encoding="utf-8"))


class LabelTest(ReviewTest):
    """A different mix that is kept on purpose gets a version label in its Rekordbox title."""

    def pl(self):
        return self.items["Pleasure Love"]["id"]

    def entry(self):
        return json.loads(review.MARKS_FILE.read_text(encoding="utf-8"))[self.pl()]

    def test_a_label_and_note_are_saved_and_survive_marking_and_unmarking(self):
        review.review_note(review.Note(id=self.pl(), label="  CZR's   Peak Hour Mix ", note="  sounds great "))
        review.review_mark(review.Mark(id=self.pl(), mark="right"))
        self.assertEqual(self.entry(), {"label": "CZR's Peak Hour Mix", "note": "sounds great", "mark": "right"})
        review.review_mark(review.Mark(id=self.pl(), mark=None))
        self.assertEqual(self.entry(), {"label": "CZR's Peak Hour Mix", "note": "sounds great"})
        item = {i["title"]: i for i in review.load_items()}["Pleasure Love"]
        self.assertEqual((item["label"], item["note"], item["mark"]), ("CZR's Peak Hour Mix", "sounds great", None))
        self.assertEqual(review.review_items()["marked"], 0, "a label alone is not a mark")

    def test_clearing_the_boxes_removes_the_entry(self):
        review.review_note(review.Note(id=self.pl(), label="x", note="y"))
        review.review_note(review.Note(id=self.pl(), label="", note=""))
        self.assertEqual(review.load_marks(), {})

    def test_apply_writes_the_title_to_rekordbox_and_the_file_then_does_nothing_more(self):
        review.review_note(review.Note(id=self.pl(), label="CZR's Peak Hour Mix"))
        with mock.patch.object(review, "_rekordbox_running", return_value=False), \
             mock.patch("services.rekordbox.set_title_by_path", return_value=True) as set_title, \
             mock.patch("services.tagging.set_title", return_value=True) as tag_title:
            result = review.review_apply_labels()
            again = review.review_apply_labels()
        self.assertEqual(result["applied"], [{"title": "Pleasure Love", "now": "Pleasure Love [CZR's Peak Hour Mix]"}])
        self.assertEqual(set_title.call_args.args[1], "Pleasure Love [CZR's Peak Hour Mix]")
        self.assertEqual(Path(tag_title.call_args.args[0]), self.new_a)
        self.assertEqual(again["applied"], [], "already applied")
        self.assertEqual(self.entry()["applied_label"], "CZR's Peak Hour Mix")

    def test_removing_a_label_puts_the_plain_title_back(self):
        review.review_note(review.Note(id=self.pl(), label="Some Mix"))
        with mock.patch.object(review, "_rekordbox_running", return_value=False), \
             mock.patch("services.rekordbox.set_title_by_path", return_value=True) as set_title, \
             mock.patch("services.tagging.set_title", return_value=True):
            review.review_apply_labels()
            review.review_note(review.Note(id=self.pl(), label=""))
            result = review.review_apply_labels()
        self.assertEqual(result["applied"][0]["now"], "Pleasure Love")
        self.assertEqual(set_title.call_args.args[1], "Pleasure Love")

    def test_it_refuses_while_rekordbox_is_open_and_skips_tracks_marked_wrong(self):
        review.review_note(review.Note(id=self.pl(), label="Some Mix"))
        with mock.patch.object(review, "_rekordbox_running", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                review.review_apply_labels()
        self.assertEqual(ctx.exception.status_code, 409)
        review.review_mark(review.Mark(id=self.pl(), mark="wrong"))
        with mock.patch.object(review, "_rekordbox_running", return_value=False), \
             mock.patch("services.rekordbox.set_title_by_path", return_value=True) as set_title:
            self.assertEqual(review.review_apply_labels()["applied"], [])
        set_title.assert_not_called()

    def test_a_track_rekordbox_does_not_have_is_reported_not_marked_applied(self):
        review.review_note(review.Note(id=self.pl(), label="Some Mix"))
        with mock.patch.object(review, "_rekordbox_running", return_value=False), \
             mock.patch("services.rekordbox.set_title_by_path", return_value=False), \
             mock.patch("services.tagging.set_title") as tag_title:
            result = review.review_apply_labels()
        self.assertEqual((result["applied"], result["failed"]), ([], ["Pleasure Love"]))
        tag_title.assert_not_called()
        self.assertNotIn("applied_label", self.entry())


class SuggestionTest(unittest.TestCase):
    def test_a_version_label_is_suggested_from_the_file_name(self):
        self.assertEqual(review.suggest_label("05 - I Feel for You (CZR\u2019s Peak Hour vocal mix)", "I Feel for You"),
                         "CZR\u2019s Peak Hour vocal mix")
        self.assertEqual(review.suggest_label("03 - Bob Sinclar - I Feel For You - CZR's Peak Hour Mix", "I Feel For You"),
                         "CZR's Peak Hour Mix")

    def test_nothing_is_suggested_when_there_is_no_new_version_in_the_name(self):
        self.assertEqual(review.suggest_label("01 - Night Surfer (Original Mix)", "Night Surfer"), "")
        self.assertEqual(review.suggest_label("Foo - Bar (Dub Mix)", "Bar (Dub Mix)"), "", "the title already has it")
        self.assertEqual(review.suggest_label("13. Supafly & De Funk - Pleasure Love", "Pleasure Love"), "")

    def test_a_different_song_is_named_from_the_file_name(self):
        self.assertEqual(review.suggest_other("13. Supafly & De Funk - Pleasure Love"), ("Supafly & De Funk", "Pleasure Love"))
        self.assertEqual(review.suggest_other("06._Babert_-_Time_After_Time_(Accapella)"), ("Babert", "Time After Time (Accapella)"))
        self.assertEqual(review.suggest_other("01 - Night Surfer"), ("", "Night Surfer"), "no artist in the name: leave it as it is")


class KeepAsDifferentSongTest(LabelTest):
    """A completely different song that is kept: renamed in Rekordbox and the file, and unlinked from the Spotify track."""

    def keep(self, artist="Babert", title="Time After Time (Accapella)"):
        review.review_note(review.Note(id=self.pl(), keep_artist=artist, keep_title=title))

    def test_keeping_marks_it_wrong_for_the_spotify_track_and_unkeeping_takes_that_back(self):
        self.keep()
        self.assertEqual(self.entry()["mark"], "wrong")
        self.assertEqual(self.entry()["keep"], {"title": "Time After Time (Accapella)", "artist": "Babert"})
        review.review_note(review.Note(id=self.pl()))
        self.assertEqual(review.load_marks(), {}, "back to untouched")

    def test_a_mark_you_set_yourself_survives_unkeeping(self):
        review.review_mark(review.Mark(id=self.pl(), mark="wrong"))
        self.keep()
        review.review_note(review.Note(id=self.pl()))
        self.assertEqual(self.entry(), {"mark": "wrong"})

    def test_apply_renames_in_rekordbox_and_the_file_and_forgets_the_spotify_link(self):
        self.keep()
        saved = []
        state = json.loads(review.STATE_FILE.read_text(encoding="utf-8"))
        fake = mock.Mock(_state=state, _save_state=lambda: saved.append(json.loads(json.dumps(state))))
        with mock.patch.object(review, "_rekordbox_running", return_value=False), \
             mock.patch.object(review, "get_orchestrator", lambda: fake), \
             mock.patch("services.rekordbox.set_title_by_path", return_value=True) as set_title, \
             mock.patch("services.tagging.set_title", return_value=True) as tag_title:
            result = review.review_apply_labels()
            again = review.review_apply_labels()
        self.assertEqual(result["applied"], [{"title": "Pleasure Love", "now": "Babert - Time After Time (Accapella)"}])
        self.assertEqual(set_title.call_args.args[1:], ("Time After Time (Accapella)", "Babert"))
        self.assertEqual(tag_title.call_args.args[1:], ("Time After Time (Accapella)", "Babert"))
        tracks = state["playlists"]["p"]["variants"]["aiff"]["tracks"]
        self.assertEqual(tracks, {}, "the Spotify track is no longer considered done, so the next sync fetches it")
        self.assertEqual(len(saved), 1)
        self.assertEqual(again["applied"], [], "applied once only")
        self.assertTrue({i["title"]: i for i in review.load_items()}["Pleasure Love"]["applied_keep"])

    def test_keeping_beats_a_label_and_a_pending_count_reflects_it(self):
        review.review_note(review.Note(id=self.pl(), label="Some Mix"))
        self.keep()
        with mock.patch.object(review, "_rekordbox_running", return_value=False), \
             mock.patch.object(review, "get_orchestrator", lambda: None), \
             mock.patch("services.rekordbox.set_title_by_path", return_value=True) as set_title, \
             mock.patch("services.tagging.set_title", return_value=True):
            review.review_apply_labels()
        self.assertEqual(set_title.call_args.args[1], "Time After Time (Accapella)", "not the labelled Spotify title")


class MoveToPlaylistTest(ReviewTest):
    """A kept song that belongs in another playlist: file, Rekordbox entry and playlist all move."""

    def pl(self):
        return self.items["Pleasure Love"]["id"]

    def entry(self):
        return json.loads(review.MARKS_FILE.read_text(encoding="utf-8"))[self.pl()]

    def setUp(self):
        super().setUp()
        state = json.loads(review.STATE_FILE.read_text(encoding="utf-8"))
        state["playlists"]["p"]["variants"]["aiff"]["display_name"] = "beachbar sets (classic) AIFF"
        state["playlists"]["p"]["variants"]["aiff"]["rb_playlist_id"] = "10"
        state["playlists"]["q"] = {"variants": {"aiff": {"display_name": "Dub Reggae Bass Addict AIFF", "rb_playlist_id": "20", "tracks": {}}}}
        review.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        self.music = self.tmp / "Incoming"
        mock.patch.object(review, "_music_folder", lambda: self.music).start()
        mock.patch.object(review, "get_orchestrator", None).start()
        # the file lives where the sync put it: in its playlist's folder
        self.home = self.music / "beachbar sets (classic) AIFF"
        self.home.mkdir(parents=True)
        moved = self.home / "a.aiff"
        self.new_a.replace(moved)
        self.new_a = moved                  # the inherited ReviewTest tests look for the file where it now is
        state = json.loads(review.STATE_FILE.read_text(encoding="utf-8"))
        state["playlists"]["p"]["variants"]["aiff"]["tracks"]["t1"]["file_path"] = moved.as_posix()
        review.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        rows = list(csv.DictReader(review.AUDIT_FILE.open(newline="", encoding="utf-8-sig")))
        rows[0]["new_file"] = str(moved)
        with review.AUDIT_FILE.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
        self.items = {i["title"]: i for i in review.load_items()}
        self.moved = moved

    def apply(self, **rb_results):
        calls = {"update": rb_results.get("update", True)}
        patches = {
            "find_or_create_playlist": mock.Mock(return_value="20"),
            "update_content_path": mock.Mock(return_value=calls["update"]),
            "find_content_by_path": mock.Mock(return_value="content-1"),
            "remove_track_from_playlist": mock.Mock(return_value=True),
            "get_playlist_track_paths": mock.Mock(return_value={"x": "1", "y": "2"}),
            "add_track_to_playlist": mock.Mock(return_value=True),
            "set_title_by_path": mock.Mock(return_value=True),
        }
        with mock.patch.object(review, "_rekordbox_running", return_value=False), \
             mock.patch("services.tagging.set_title", return_value=True), \
             mock.patch.multiple("services.rekordbox", **patches):
            result = review.review_apply_labels()
        return result, patches

    def test_the_playlist_menu_lists_the_playlists_sff_has_made(self):
        self.assertEqual(review.review_playlists()["playlists"], ["Dub Reggae Bass Addict AIFF", "beachbar sets (classic) AIFF"])

    def test_the_choice_is_saved_with_the_song(self):
        review.review_note(review.Note(id=self.pl(), keep_title="Only Love", keep_move="Dub Reggae Bass Addict AIFF"))
        self.assertEqual(self.entry()["keep"]["move_to"], "Dub Reggae Bass Addict AIFF")
        self.assertEqual({i["title"]: i for i in review.load_items()}["Pleasure Love"]["keep_move"], "Dub Reggae Bass Addict AIFF")

    def test_apply_moves_the_file_the_entry_and_the_playlist_membership(self):
        review.review_note(review.Note(id=self.pl(), keep_artist="Saint Etienne", keep_title="Only Love Can Break Your Heart",
                                       keep_move="Dub Reggae Bass Addict AIFF"))
        result, rb = self.apply()
        dest = self.music / "Dub Reggae Bass Addict AIFF" / "a.aiff"
        self.assertTrue(dest.is_file() and not self.moved.exists(), "the file is now in the target playlist's folder")
        rb["update_content_path"].assert_called_once_with(self.moved.as_posix(), dest.as_posix())
        rb["remove_track_from_playlist"].assert_called_once_with("beachbar sets (classic) AIFF", "a.aiff")
        rb["add_track_to_playlist"].assert_called_once_with("20", "content-1", 3)          # at the end
        self.assertIn("Dub Reggae Bass Addict AIFF", result["applied"][0]["now"])
        self.assertEqual(result["failed"], [])
        state = json.loads(review.STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(state["playlists"]["q"]["variants"]["aiff"]["tracks"], {}, "NOT recorded against the target's Spotify list")
        self.assertEqual(state["playlists"]["p"]["variants"]["aiff"]["tracks"], {}, "and no longer tied to the Spotify track")

    def test_if_rekordbox_refuses_the_new_path_the_file_goes_back_where_it_was(self):
        review.review_note(review.Note(id=self.pl(), keep_title="Only Love", keep_move="Dub Reggae Bass Addict AIFF"))
        result, rb = self.apply(update=False)
        self.assertTrue(self.moved.is_file(), "put back: Rekordbox must keep pointing at a real file")
        self.assertFalse((self.music / "Dub Reggae Bass Addict AIFF" / "a.aiff").exists())
        rb["add_track_to_playlist"].assert_not_called()
        self.assertTrue(any("could not be moved" in f for f in result["failed"]), result["failed"])

    def test_without_a_target_nothing_moves(self):
        review.review_note(review.Note(id=self.pl(), keep_title="Only Love"))
        result, rb = self.apply()
        self.assertTrue(self.moved.is_file())
        rb["update_content_path"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
