import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import app_config
from services.sync_soulseek import take_over_replaced_playlists
from tests.test_extra_playlists import FakeSp, lib, service

OLD, NEW, OTHER, FUTURE = "O" * 22, "N" * 22, "D" * 22, "F" * 22


def info(pid, name):
    return {"id": pid, "name": name, "snapshot_id": "s9", "owner": {"display_name": "Zayan Khan"}}


class ReplacingAPlaylistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        mock.patch.object(app_config, "CONFIG_FILE", self.tmp / "app_config.json").start()
        self.addCleanup(mock.patch.stopall)
        app_config.add_extra_playlist(NEW, "the-paris-drive", "the-paris-drive", replaces=OLD)

    def library(self, *extra):
        return [lib(OLD, "FF the-paris-drive", "Me"), lib(OTHER, "FF Dub", "Me"), *extra]

    def test_the_followed_playlist_takes_the_place_of_the_ff_copy(self):
        svc = service(FakeSp(library=self.library(), readable={NEW: info(NEW, "the-paris-drive")}))
        self.assertEqual([(p["display_name"], p["id"]) for p in svc.get_all_playlists()],
                         [("Dub", OTHER), ("the-paris-drive", NEW)])

    def test_other_and_future_ff_playlists_are_still_found(self):
        svc = service(FakeSp(library=self.library(lib(FUTURE, "FF Warm", "Me")), readable={NEW: info(NEW, "the-paris-drive")}))
        ids = [p["id"] for p in svc.get_all_playlists()]
        self.assertIn(OTHER, ids)
        self.assertIn(FUTURE, ids)
        self.assertNotIn(OLD, ids)

    def test_the_ff_copy_stays_if_the_replacement_cannot_be_read(self):
        svc = service(FakeSp(library=self.library()))            # NEW is not readable
        ids = [p["id"] for p in svc.get_all_playlists()]
        self.assertIn(OLD, ids, "nothing may drop out of your library because a swap failed")
        self.assertEqual(len(svc.skipped), 1)

    def test_a_playlist_that_replaces_nothing_changes_nothing(self):
        app_config.remove_extra_playlist(NEW)
        app_config.add_extra_playlist(NEW, "the-paris-drive", "the-paris-drive")
        svc = service(FakeSp(library=self.library(), readable={NEW: info(NEW, "the-paris-drive")}))
        self.assertEqual({p["id"] for p in svc.get_all_playlists()}, {OLD, OTHER, NEW})


class RecordsMoveTest(unittest.TestCase):
    def state(self):
        return {"playlists": {
            OLD: {"tracks": {}, "snapshot_id": "a", "name": "FF the-paris-drive ", "display_name": "the-paris-drive",
                  "variants": {"aiff": {"marker": 1}}},
            OTHER: {"tracks": {}, "snapshot_id": "b", "name": "FF Dub", "display_name": "Dub", "variants": {}}}}

    def pls(self):
        return [{"id": OTHER, "name": "FF Dub", "display_name": "Dub"},
                {"id": NEW, "name": "the-paris-drive", "display_name": "the-paris-drive", "replaces": OLD}]

    def test_the_records_move_to_the_new_id_so_nothing_is_redone(self):
        st = self.state()
        self.assertEqual(take_over_replaced_playlists(st, self.pls()), ["the-paris-drive"])
        self.assertNotIn(OLD, st["playlists"])
        self.assertEqual(st["playlists"][NEW]["variants"], {"aiff": {"marker": 1}})
        self.assertEqual(st["playlists"][NEW]["name"], "the-paris-drive")
        self.assertIn(OTHER, st["playlists"], "other playlists are untouched")

    def test_it_happens_once(self):
        st = self.state()
        take_over_replaced_playlists(st, self.pls())
        st["playlists"][NEW]["variants"]["aiff"]["marker"] = 2
        self.assertEqual(take_over_replaced_playlists(st, self.pls()), [])
        self.assertEqual(st["playlists"][NEW]["variants"]["aiff"]["marker"], 2)

    def test_a_playlist_with_records_of_its_own_is_never_overwritten(self):
        st = self.state()
        st["playlists"][NEW] = {"tracks": {}, "snapshot_id": "", "name": "x", "display_name": "x", "variants": {}}
        self.assertEqual(take_over_replaced_playlists(st, self.pls()), [])
        self.assertIn(OLD, st["playlists"])

    def test_nothing_to_move_when_the_old_playlist_was_never_synced(self):
        st = {"playlists": {}}
        self.assertEqual(take_over_replaced_playlists(st, self.pls()), [])


if __name__ == "__main__":
    unittest.main()
