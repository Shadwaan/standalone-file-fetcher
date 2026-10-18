import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import audio_formats, sync_soulseek
from services.labels import norm_title
from tests.test_soulseek_sync import make_audio, spotify_track


class NormTitleTest(unittest.TestCase):
    def test_bracket_kind_case_and_punctuation_do_not_matter(self):
        self.assertEqual(norm_title("Oh Mama [Dub]"), norm_title("oh mama (dub)"))
        self.assertEqual(norm_title("Don't Stop!"), norm_title("Dont Stop"))

    def test_a_feat_credit_is_ignored(self):
        self.assertEqual(norm_title("Oh Mama (feat. Brushy One String) (Dub)"), norm_title("Oh Mama (Dub)"))
        self.assertEqual(norm_title("Back & Forth ft. K.E"), norm_title("Back & Forth"))

    def test_the_version_is_NOT_ignored(self):
        self.assertNotEqual(norm_title("Oh Mama (Dub)"), norm_title("Oh Mama"))
        self.assertNotEqual(norm_title("Horny (Radio Edit)"), norm_title("Horny (Extended Mix)"))


class SameRecordingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_same_audio_in_another_format_is_the_same_recording(self):
        a, b = self.tmp / "a.flac", self.tmp / "b.flac"
        make_audio(a, "song_a"); shutil.copy(a, b)
        self.assertTrue(audio_formats.same_recording(a, b))

    def test_a_different_song_is_not(self):
        a, b = self.tmp / "a.flac", self.tmp / "b.flac"
        make_audio(a, "song_a"); make_audio(b, "song_b")
        self.assertFalse(audio_formats.same_recording(a, b))

    def test_an_unreadable_file_is_never_called_the_same(self):
        junk = self.tmp / "junk.flac"; junk.write_bytes(b"not audio")
        self.assertFalse(audio_formats.same_recording(junk, junk))


class NoSilentCopyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.folder = self.tmp / "Dub AIFF"; self.folder.mkdir()

    def test_the_same_recording_is_reused_instead_of_saved_as_name_1(self):
        existing = self.folder / "Song.flac"; make_audio(existing, "song_a")
        new = self.tmp / "dl" / "Song.flac"; make_audio(new, "song_a")
        dest, reuse = audio_formats.dest_or_existing(self.folder, "Song", "flac", new)
        self.assertEqual((dest, reuse), (existing, True))

    def test_different_audio_with_the_same_name_still_gets_a_suffix(self):
        make_audio(self.folder / "Song.flac", "song_a")
        new = self.tmp / "dl" / "Song.flac"; make_audio(new, "song_b")
        dest, reuse = audio_formats.dest_or_existing(self.folder, "Song", "flac", new)
        self.assertEqual((dest.name, reuse), ("Song_1.flac", False))

    def test_a_match_further_down_the_suffixes_is_found(self):
        make_audio(self.folder / "Song.flac", "song_b")
        make_audio(self.folder / "Song_1.flac", "song_a")
        new = self.tmp / "dl" / "Song.flac"; make_audio(new, "song_a")
        dest, reuse = audio_formats.dest_or_existing(self.folder, "Song", "flac", new)
        self.assertEqual((dest.name, reuse), ("Song_1.flac", True))

    def test_a_free_name_is_simply_used(self):
        new = self.tmp / "dl" / "Song.flac"; make_audio(new, "song_a")
        dest, reuse = audio_formats.dest_or_existing(self.folder, "Song", "flac", new)
        self.assertEqual((dest.name, reuse), ("Song.flac", False))

    def test_build_outputs_makes_no_second_copy_of_a_recording_already_there(self):
        existing = self.folder / "Song.flac"; make_audio(existing, "song_a")
        src = self.tmp / "dl" / "Song.flac"; make_audio(src, "song_a")
        outputs = audio_formats.build_outputs(src, ["flac"], lambda fmt: self.folder, self.tmp / "originals")
        self.assertEqual(outputs, {"flac": existing})
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["Song.flac"], "no Song_1")


class HeldFileGuardTest(unittest.TestCase):
    """A download that is the same recording as the stand-in you already hold is a duplicate, not the real mix."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.track = spotify_track(0, "id-mama", "Oh Mama", "Austin Ato", 0)
        self.held = self.tmp / "Oh Mama (Dub).flac"; make_audio(self.held, "song_a")

    def validate(self, path):
        return sync_soulseek._make_validator({}, {}, None, {"id-mama": [self.held]})(path, self.track)

    def test_the_same_recording_is_turned_down(self):
        again = self.tmp / "dl" / "Oh Mama.flac"; make_audio(again, "song_a")
        self.assertEqual(self.validate(again), "the same recording you already have for this track")

    def test_a_genuinely_different_mix_is_accepted(self):
        other = self.tmp / "dl" / "Oh Mama.flac"; make_audio(other, "song_b")
        self.assertIs(self.validate(other), True)

    def test_held_files_are_the_stand_ins_recorded_for_the_track(self):
        state = {"p": {"variants": {"aiff": {"tracks": {
            "id-mama": {"stand_in": True, "file_path": str(self.held)},
            "id-done": {"file_path": str(self.held)}}}}}}
        self.assertEqual(sync_soulseek._held_files(state, ["id-mama", "id-done"]), {"id-mama": [self.held]},
                         "only a stand-in counts; a finished track needs no check")


class DuplicateReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_name_1_copy_that_sounds_the_same_is_reported(self):
        a, b = self.tmp / "Song.flac", self.tmp / "Song_1.flac"
        make_audio(a, "song_a"); shutil.copy(a, b)
        self.assertEqual(sync_soulseek.find_suffix_duplicates(self.tmp), [(a, b)])

    def test_a_name_1_file_that_is_a_different_recording_is_not(self):
        make_audio(self.tmp / "Song.flac", "song_a"); make_audio(self.tmp / "Song_1.flac", "song_b")
        self.assertEqual(sync_soulseek.find_suffix_duplicates(self.tmp), [])

    def test_a_missing_folder_is_fine(self):
        self.assertEqual(sync_soulseek.find_suffix_duplicates(self.tmp / "nope"), [])


class NamedEditTest(unittest.TestCase):
    """A plain "Radio Edit" must not be satisfied by somebody else's named edit (the Hardwell case)."""
    TITLE = "Man With The Red Face - Radio Edit"

    def ok(self, path):
        from services.soulseek import passes_version_guard
        return passes_version_guard(path.lower(), self.TITLE)

    def test_a_named_radio_edit_is_not_the_plain_one(self):
        self.assertFalse(self.ok("x/Mark Knight & Funkagenda - Man With The Red Face (Hardwell Radio Edit).flac"))

    def test_the_plain_radio_edit_is_fine(self):
        self.assertTrue(self.ok("x/Mark Knight & Funkagenda - Man With The Red Face (Radio Edit).flac"))
        self.assertTrue(self.ok("x/Man With The Red Face [Radio Edit] 2013.flac"))

    def test_a_named_version_that_was_asked_for_is_still_found(self):
        from services.soulseek import passes_version_guard
        self.assertTrue(passes_version_guard("x/foo (hardwell radio edit).flac", "Foo - Hardwell Radio Edit"))

    def test_a_kind_of_version_is_not_a_name(self):
        self.assertTrue(self.ok("x/Man With The Red Face (Original Radio Edit).flac"))


class MergeCommentTest(unittest.TestCase):
    def test_a_comment_is_added_after_what_is_already_there(self):
        from services.rekordbox import merge_comment
        self.assertEqual(merge_comment("", "Bangkok Groove"), "Bangkok Groove")
        self.assertEqual(merge_comment("rip: vinyl", "Bangkok Groove"), "rip: vinyl | Bangkok Groove")

    def test_the_same_comment_is_not_added_twice(self):
        from services.rekordbox import merge_comment
        self.assertEqual(merge_comment("Bangkok Groove", "bangkok groove"), "Bangkok Groove")

    def test_an_empty_comment_changes_nothing(self):
        from services.rekordbox import merge_comment
        self.assertEqual(merge_comment("keep me", ""), "keep me")
        self.assertEqual(merge_comment(None, None), "")


if __name__ == "__main__":
    unittest.main()
