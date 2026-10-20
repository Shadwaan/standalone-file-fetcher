import csv
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services import app_config, downloader, soulseek, youtube_fill
from services import rekordbox as rb
from tests.test_soulseek_sync import SyncFixture, make_audio, spotify_track


def track(title="Ghost Song", artist="Nobody"):
    return SimpleNamespace(title=title, artist=artist, duration_ms=0)


class QualityLabelTest(unittest.TestCase):
    def test_the_label_says_what_youtube_really_served(self):
        self.assertEqual(youtube_fill.quality_label("opus", 130), "YT opus 130k")
        self.assertEqual(youtube_fill.quality_label("aac", 128.4), "YT aac 128k")
        self.assertEqual(youtube_fill.quality_label("", None), "YT")

    def test_source_quality_reads_the_codec_and_bitrate_yt_dlp_reports(self):
        self.assertEqual(downloader.source_quality({"acodec": "opus", "abr": 129.9}), {"codec": "opus", "abr": 130})
        self.assertEqual(downloader.source_quality({"acodec": "mp4a.40.2", "abr": 129.5}), {"codec": "aac", "abr": 130})
        self.assertEqual(downloader.source_quality({}), {"codec": "audio", "abr": None})


class CandidateTest(unittest.TestCase):
    def fits(self, video_title, uploader="", title="Ghost Song", artist="Nobody"):
        return youtube_fill.candidate_fits({"title": video_title, "uploader": uploader}, track(title, artist))

    def test_the_right_video_is_accepted(self):
        self.assertTrue(self.fits("Nobody - Ghost Song (Official Audio)"))
        self.assertTrue(self.fits("Ghost Song", uploader="Nobody - Topic"))

    def test_live_covers_slowed_and_stems_are_not(self):
        for bad in ("Nobody - Ghost Song (Live at Fabric)", "Ghost Song cover by someone", "Nobody - Ghost Song (slowed + reverb)",
                    "Nobody - Ghost Song (Acapella)", "Nobody - Ghost Song nightcore"):
            self.assertFalse(self.fits(bad), bad)

    def test_a_video_that_does_not_carry_the_songs_words_is_not(self):
        self.assertFalse(self.fits("Nobody - Something Else Entirely"))
        self.assertFalse(self.fits("Ghost Song", uploader="Some Other Artist"))

    def test_a_named_remix_the_title_did_not_ask_for_is_not(self):
        self.assertFalse(self.fits("Nobody - Ghost Song (Someone Remix)"))

    def test_a_live_title_does_ask_for_live(self):
        self.assertTrue(self.fits("Nobody - Ghost Song (Live)", title="Ghost Song (Live)"))


class YoutubeFillInTest(SyncFixture):
    def setUp(self):
        super().setUp()
        from services import app_config as cfg
        cfg.set_output_formats(["aiff"])
        self.fill_calls = []
        self.spotify_tracks = [spotify_track(0, "id-ghost", "Ghost Song", "Nobody", 0)]
        mock.patch.object(rb, "update_content_path", return_value=True).start()       # never the real library
        outer = self

        def fake_fill(track, work_dir, reference=None):
            outer.fill_calls.append(track.title)
            p = Path(work_dir) / f"{track.safe_filename}.mp3"
            make_audio(p, "song_a_mp3")
            return {"path": p, "url": "https://youtu.be/abc", "video_title": "Nobody - Ghost Song (Official Audio)",
                    "codec": "opus", "abr": 130, "label": "YT opus 130k"}
        mock.patch("services.youtube_fill.fill_in", fake_fill).start()

    def enable(self):
        cfg = app_config.load()
        cfg["youtube_fallback"] = True
        app_config.save(cfg)

    def titles(self, name="Deep tech AIFF"):
        pl = next(p for p in self.rbfake.playlists.values() if p["name"] == name)
        return sorted(self.rbfake.contents[c]["title"] for c in pl["tracks"])

    def record(self):
        state = json.loads((self.tmp / "sync_state.json").read_text(encoding="utf-8"))
        return next(iter(next(iter(state["playlists"].values()))["variants"]["aiff"]["tracks"].values()))

    def test_it_is_off_unless_switched_on(self):
        result = self.run_sync()
        self.assertEqual((self.fill_calls, result["tracks_failed"]), ([], 1))

    def test_a_track_nothing_else_could_supply_is_filled_in_and_labelled_with_what_it_really_is(self):
        self.enable()
        result = self.run_sync()
        self.assertEqual(self.fill_calls, ["Ghost Song"])
        self.assertEqual(self.titles(), ["Ghost Song [YT opus 130k]"], "the playlist says what it is when it is played")
        self.assertEqual(result["tracks_failed"], 0)
        files = list((self.music / "Deep tech AIFF").glob("*.mp3"))
        self.assertEqual(len(files), 1, "kept as the MP3 it is, not dressed up as AIFF")
        rec = self.record()
        self.assertEqual((rec["title"], rec["stand_in"], rec["stand_in_label"], rec["source"]["how"]),
                         ("Ghost Song", True, "YT opus 130k", "youtube"))

    def test_it_is_queued_for_the_review_page_so_a_wrong_video_can_be_caught_by_ear(self):
        self.enable()
        self.run_sync()
        with (self.tmp / "audit.csv").open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertIn("YouTube fill-in (YT opus 130k)", rows[0]["verdict"])

    def test_the_next_sync_does_not_download_it_again(self):
        self.enable()
        self.run_sync()
        (self.tmp / "failed_tracks.json").unlink(missing_ok=True)
        self.run_sync()
        self.assertEqual(self.fill_calls, ["Ghost Song"], "a track that has a file is not filled in twice")
        self.assertEqual(len(list((self.music / "Deep tech AIFF").glob("*.mp3"))), 1)

    def test_when_the_real_lossless_copy_arrives_it_replaces_the_youtube_one(self):
        self.enable()
        self.run_sync()
        yt_file = next((self.music / "Deep tech AIFF").glob("*.mp3"))
        (self.tmp / "failed_tracks.json").unlink(missing_ok=True)
        self.nico.offer("Nobody", "Ghost Song", "song_a", "u1", r"m\01 - Ghost Song.flac", {"4": 44100, "5": 16})
        self.run_sync()
        self.assertEqual(self.titles(), ["Ghost Song"], "the real one, plain")
        self.assertIn(("Deep tech AIFF", yt_file.name), self.rbfake.removed, "the YouTube stand-in left the playlist")
        self.assertFalse(yt_file.exists())
        self.assertTrue((self.music / "Deep tech AIFF" / "_replaced" / yt_file.name).is_file(), "kept, never deleted")
        self.assertNotIn("stand_in", self.record())
        self.assertEqual(self.fill_calls, ["Ghost Song"])

    def test_a_track_that_already_has_a_stand_in_mix_is_not_filled_in(self):
        self.enable()
        existing = self.tmp / "Library" / "x.flac"
        make_audio(existing, "song_b")
        self.rbfake.add_existing_playlist("Deep tech AIFF", {"Ghost Song [other mix]": str(existing).replace("\\", "/")})
        self.run_sync()
        self.assertEqual(self.fill_calls, [], "it has a file; only Soulseek is still being asked for the real one")

    def test_a_failing_youtube_lookup_never_stops_the_sync(self):
        self.enable()
        with mock.patch("services.youtube_fill.fill_in", side_effect=RuntimeError("yt-dlp broke")):
            result = self.run_sync()
        self.assertEqual(result["tracks_failed"], 1)


if __name__ == "__main__":
    unittest.main()
