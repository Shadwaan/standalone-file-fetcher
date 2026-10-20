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

    def test_a_filled_in_track_stays_on_the_failure_record_so_it_is_not_searched_again_straight_away(self):
        self.enable()
        self.run_sync()
        failed = json.loads((self.tmp / "failed_tracks.json").read_text(encoding="utf-8"))
        self.assertIn("id-ghost", failed, "Soulseek's failure is remembered, and it is on the to-buy list")
        self.nico.searches.clear()
        self.run_sync()                                  # the failure record is NOT cleared this time
        self.assertEqual(self.nico.searches, [], "no Soulseek search for a track that just failed")
        self.assertEqual(self.fill_calls, ["Ghost Song"])

    def test_the_to_buy_list_still_shows_a_track_that_has_a_youtube_copy(self):
        from services import failure_log, to_buy
        self.enable()
        self.run_sync()
        state = json.loads((self.tmp / "sync_state.json").read_text(encoding="utf-8"))
        (row,) = to_buy.build_list(state, failure_log.all_failures())
        self.assertEqual((row["title"], row["youtube_copy"]), ("Ghost Song", "YT opus 130k"))

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

    def fail_it_recently(self):
        """Soulseek was already asked and found nothing: the track is inside its 'failed recently' cooldown."""
        from datetime import datetime
        (self.tmp / "failed_tracks.json").write_text(json.dumps({"id-ghost": {
            "spotify_id": "id-ghost", "artist": "Nobody", "title": "Ghost Song", "playlist": "Deep tech AIFF",
            "category": "no results", "detail": "the network returned no results for it", "attempts": 4,
            "first_failed": datetime.now().isoformat(), "last_failed": datetime.now().isoformat(), "times_failed": 3}}), encoding="utf-8")

    def test_a_track_soulseek_already_failed_for_goes_straight_to_youtube_without_searching_again(self):
        self.enable()
        self.fail_it_recently()
        result = self.run_sync()
        self.assertEqual(self.nico.searches, [], "not searched on Soulseek again")
        self.assertEqual(self.fill_calls, ["Ghost Song"])
        self.assertEqual(self.titles(), ["Ghost Song [YT opus 130k]"])
        self.assertEqual(result["tracks_deferred"], 0)

    def test_an_old_failure_can_be_treated_as_checked_for_one_run(self):
        from datetime import datetime, timedelta
        self.enable()
        self.fail_it_recently()
        path = self.tmp / "failed_tracks.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["id-ghost"]["last_failed"] = (datetime.now() - timedelta(days=10)).isoformat()
        path.write_text(json.dumps(data), encoding="utf-8")
        cfg = app_config.load(); cfg["failed_cooldown_hours"] = 24 * 30; app_config.save(cfg)
        self.run_sync()
        self.assertEqual(self.nico.searches, [], "10 days ago is inside the 30-day window set for this run")
        self.assertEqual(self.fill_calls, ["Ghost Song"])
        self.assertIsNone(app_config.get_failed_cooldown_hours(), "for this run only: it resets itself")

    def test_without_that_setting_an_old_failure_is_searched_again(self):
        from datetime import datetime, timedelta
        self.enable()
        self.fail_it_recently()
        path = self.tmp / "failed_tracks.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["id-ghost"]["last_failed"] = (datetime.now() - timedelta(days=10)).isoformat()
        path.write_text(json.dumps(data), encoding="utf-8")
        self.run_sync()
        self.assertTrue(self.nico.searches, "the normal 24 h cooldown has passed")

    def test_a_track_that_is_not_on_youtube_either_is_not_looked_up_on_every_sync(self):
        self.enable()
        self.fail_it_recently()
        with mock.patch("services.youtube_fill.fill_in", return_value=None) as lookup:
            self.run_sync()
            self.run_sync()
        self.assertEqual(lookup.call_count, 1, "remembered as a miss for a week")

    def test_a_youtube_error_is_not_remembered_as_a_miss(self):
        self.enable()
        self.fail_it_recently()
        with mock.patch("services.youtube_fill.fill_in", side_effect=RuntimeError("offline")) as lookup:
            self.run_sync()
            self.run_sync()
        self.assertEqual(lookup.call_count, 2)

    def test_the_same_track_in_a_second_playlist_reuses_the_youtube_copy(self):
        self.enable()
        self.playlist_names = {"PL1": "Deep tech", "PL2": "Dub"}
        self.run_sync()
        self.assertEqual(self.fill_calls, ["Ghost Song"], "downloaded from YouTube once, not once per playlist")
        self.assertEqual(self.titles("Deep tech AIFF"), ["Ghost Song [YT opus 130k]"])
        self.assertEqual(self.titles("Dub AIFF"), ["Ghost Song [YT opus 130k]"], "both playlists have it, labelled")
        mp3s = list(self.music.glob("*/*.mp3"))
        self.assertEqual(len(mp3s), 1, "one file shared by both playlists")

    def test_a_playlist_can_be_left_out_of_one_run_and_comes_back_after(self):
        self.enable()
        self.playlist_names = {"PL1": "Deep tech", "PL2": "Dub Reggae"}
        cfg = app_config.load(); cfg["skip_playlists_once"] = ["dub reggae"]; app_config.save(cfg)
        result = self.run_sync()
        self.assertEqual([p["name"] for p in self.rbfake.playlists.values()], ["Deep tech AIFF"], "the skipped one was not touched")
        self.assertTrue(any("Dub Reggae" in e for e in result["errors"]))
        self.assertEqual(app_config.get_skip_playlists_once(), [], "for this run only")
        self.run_sync()
        self.assertEqual(sorted(p["name"] for p in self.rbfake.playlists.values()), ["Deep tech AIFF", "Dub Reggae AIFF"])

    def test_in_a_quick_run_a_stand_in_mix_is_not_searched_for_either(self):
        existing = self.tmp / "Library" / "x.flac"
        make_audio(existing, "song_b")
        self.rbfake.add_existing_playlist("Deep tech AIFF", {"Ghost Song [other mix]": str(existing).replace("\\", "/")})
        self.run_sync()
        self.assertTrue(self.nico.searches, "normally a stand-in is looked up again on every sync")
        self.nico.searches.clear()
        (self.tmp / "failed_tracks.json").unlink(missing_ok=True)
        cfg = app_config.load(); cfg["failed_cooldown_hours"] = 24 * 30; app_config.save(cfg)
        self.run_sync()
        self.assertEqual(self.nico.searches, [], "a quick run leaves it alone")

    def test_the_quick_limits_apply_only_inside_the_block(self):
        before = (soulseek.MAX_ATTEMPTS_PER_TRACK, soulseek.QUEUE_PATIENCE_SECONDS)
        with soulseek.quick_limits():
            self.assertLess(soulseek.MAX_ATTEMPTS_PER_TRACK, before[0])
            self.assertLess(soulseek.QUEUE_PATIENCE_SECONDS, before[1])
        self.assertEqual((soulseek.MAX_ATTEMPTS_PER_TRACK, soulseek.QUEUE_PATIENCE_SECONDS), before)

    def test_a_rejected_recording_already_in_the_nicotine_folder_is_not_taken_back_in(self):
        bad = self.tmp / "Library" / "_rejected" / "Ghost Song.flac"
        make_audio(bad, "song_a")
        make_audio(self.nic / "01 - Ghost Song.flac", "song_a")             # an earlier run's download, same wrong recording
        with mock.patch("services.sync_soulseek._rejected_files", return_value={"id-ghost": [bad]}):
            result = self.run_sync()
        self.assertEqual(self.titles_or_none(), [], "not imported")
        self.assertEqual(result["tracks_failed"], 1)

    def test_a_different_recording_already_in_the_nicotine_folder_is_used(self):
        bad = self.tmp / "Library" / "_rejected" / "Ghost Song.flac"
        make_audio(bad, "song_a")
        make_audio(self.nic / "01 - Ghost Song.flac", "song_b")
        with mock.patch("services.sync_soulseek._rejected_files", return_value={"id-ghost": [bad]}):
            result = self.run_sync()
        self.assertEqual(self.titles(), ["Ghost Song"])
        self.assertEqual(result["tracks_failed"], 0)

    def titles_or_none(self):
        try:
            return self.titles()
        except StopIteration:
            return []

    def test_a_failing_youtube_lookup_never_stops_the_sync(self):
        self.enable()
        with mock.patch("services.youtube_fill.fill_in", side_effect=RuntimeError("yt-dlp broke")):
            result = self.run_sync()
        self.assertEqual(result["tracks_failed"], 1)


if __name__ == "__main__":
    unittest.main()
