import unittest
from types import SimpleNamespace
from unittest import mock

from services import rekordbox as rb


class FakeDb:
    def __init__(self, fail=False):
        self.fail, self.calls, self.rolled_back = fail, [], False
        self.session = SimpleNamespace(rollback=lambda: setattr(self, "rolled_back", True))

    def add_content(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if self.fail:
            raise ValueError("Invalid file type: .xyz")
        return SimpleNamespace(FolderPath="D:\\Music\\a.aiff", StockDate=None, DateCreated=None, ID="1", UUID="u")


class ContentViaAddContentTest(unittest.TestCase):
    def test_the_row_comes_from_add_content_with_the_parity_fields_and_a_forward_slash_path(self):
        db = FakeDb()
        row = rb._content_via_add_content(db, "D:/Music/a.aiff", "A Title", 1411, 44100, "artist-1", "album-1")
        (path, kw), = db.calls
        self.assertEqual(path, "D:/Music/a.aiff")
        self.assertEqual((kw["Title"], kw["BitRate"], kw["SampleRate"], kw["Analysed"], kw["ArtistID"], kw["AlbumID"]),
                         ("A Title", 1411, 44100, 0, "artist-1", "album-1"))
        self.assertNotIn("usn", kw, "the hand-built rows' usn = 0 is gone")
        for taken_by_add_content in ("ID", "UUID", "FolderPath", "FileType", "DeviceID", "MasterDBID", "HotCueAutoLoad"):
            self.assertNotIn(taken_by_add_content, kw)
        self.assertEqual(row.FolderPath, "D:/Music/a.aiff", "Rekordbox keeps forward slashes, even on Windows")
        self.assertTrue(row.StockDate and row.DateCreated)

    def test_when_add_content_cannot_make_the_row_it_says_so_and_rolls_back_instead_of_failing(self):
        db = FakeDb(fail=True)
        self.assertIsNone(rb._content_via_add_content(db, "D:/Music/a.xyz", "T", 1, 1, None, None))
        self.assertTrue(db.rolled_back)

    def test_no_artist_or_album_is_simply_left_out(self):
        db = FakeDb()
        rb._content_via_add_content(db, "D:/Music/a.aiff", "T", 1, 1, None, None)
        self.assertNotIn("ArtistID", db.calls[0][1])
        self.assertNotIn("AlbumID", db.calls[0][1])


if __name__ == "__main__":
    unittest.main()
