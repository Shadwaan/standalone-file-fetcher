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


if __name__ == "__main__":
    unittest.main()
