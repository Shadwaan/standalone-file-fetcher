import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import app_config, soulseek
from services import spotify as spotify_module
from services.spotify import SpotifyService, parse_playlist_ref
from tests.test_soulseek_sync import SyncFixture, spotify_track

ID = "37i9dQZF1DXcBWIGoYBM5M"                       # 22 characters, like a real playlist id


class ParseRefTest(unittest.TestCase):
    def test_a_link_with_tracking_text_gives_the_id(self):
        self.assertEqual(parse_playlist_ref(f"https://open.spotify.com/playlist/{ID}?si=abc123def"), ID)

    def test_a_localised_link_a_uri_and_a_bare_id_work_too(self):
        self.assertEqual(parse_playlist_ref(f"https://open.spotify.com/intl-de/playlist/{ID}"), ID)
        self.assertEqual(parse_playlist_ref(f"spotify:playlist:{ID}"), ID)
        self.assertEqual(parse_playlist_ref(f"  {ID}  "), ID)

    def test_a_name_is_not_an_id(self):
        self.assertIsNone(parse_playlist_ref("the-paris-drive"))
        self.assertIsNone(parse_playlist_ref(""))
        self.assertIsNone(parse_playlist_ref("https://example.com/playlist/" + ID))


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        mock.patch.object(app_config, "CONFIG_FILE", self.tmp / "app_config.json").start()
        self.addCleanup(mock.patch.stopall)

    def test_following_stores_the_playlist_and_a_default_name(self):
        app_config.add_extra_playlist(ID, "the-paris-drive")
        self.assertEqual(app_config.get_extra_playlists(), [{"id": ID, "name": "the-paris-drive", "display_name": "the-paris-drive"}])

    def test_a_chosen_name_is_used_and_following_twice_does_not_duplicate(self):
        app_config.add_extra_playlist(ID, "the-paris-drive", "Paris Drive")
        app_config.add_extra_playlist(ID, "the-paris-drive", "Paris")
        self.assertEqual([p["display_name"] for p in app_config.get_extra_playlists()], ["Paris"])

    def test_unfollowing_removes_it_and_says_whether_it_was_there(self):
        app_config.add_extra_playlist(ID, "x")
        self.assertTrue(app_config.remove_extra_playlist(ID))
        self.assertFalse(app_config.remove_extra_playlist(ID))
        self.assertEqual(app_config.get_extra_playlists(), [])

    def test_other_settings_survive(self):
        app_config.set_output_formats(["aiff"])
        app_config.add_extra_playlist(ID, "x")
        self.assertEqual(app_config.get_output_formats(), ["aiff"])


class FakeSp:
    """Enough of spotipy for the playlist lookups."""

    def __init__(self, library=None, readable=None, tracks_blocked=()):
        self.library = library or []                 # playlists in the user's library (any owner)
        self.readable = readable or {}               # id -> info for playlists that can be read by id
        self.tracks_blocked = set(tracks_blocked)    # playlists whose TRACKS Spotify refuses (somebody else's)

    def playlist_items(self, playlist_id, limit=100, additional_types=None):
        if playlist_id in self.tracks_blocked:
            raise RuntimeError("http status: 403 - Forbidden")
        return {"items": [], "total": 0}

    def current_user_playlists(self, limit=50):
        return {"items": self.library, "next": None}

    def next(self, results):
        return None

    def playlist(self, playlist_id, fields=None):
        if playlist_id not in self.readable:
            raise RuntimeError("403 Forbidden")
        return self.readable[playlist_id]


def service(sp, prefix="FF"):
    svc = SpotifyService.__new__(SpotifyService)       # no sign-in
    svc.sp, svc.prefix = sp, prefix
    return svc


def lib(pid, name, owner="Zayan Khan"):
    return {"id": pid, "name": name, "snapshot_id": "s", "owner": {"display_name": owner}, "tracks": {"total": 5}}


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        mock.patch.object(app_config, "CONFIG_FILE", self.tmp / "app_config.json").start()
        self.addCleanup(mock.patch.stopall)

    def test_a_playlist_in_your_library_is_found_by_its_exact_name_whoever_made_it(self):
        svc = service(FakeSp(library=[lib("A" * 22, "FF Dub", "Me"), lib(ID, "the-paris-drive")]))
        found = svc.resolve_playlist("The-Paris-Drive")
        self.assertEqual((found["id"], found["name"], found["owner"]), (ID, "the-paris-drive", "Zayan Khan"))

    def test_a_link_is_looked_up_by_id(self):
        svc = service(FakeSp(readable={ID: {"id": ID, "name": "the-paris-drive", "snapshot_id": "s", "owner": {"display_name": "Zayan Khan"}}}))
        self.assertEqual(svc.resolve_playlist(f"https://open.spotify.com/playlist/{ID}?si=x")["name"], "the-paris-drive")

    def test_an_unknown_name_or_a_blocked_link_gives_a_clear_error(self):
        svc = service(FakeSp(library=[lib("A" * 22, "FF Dub")]))
        with self.assertRaises(ValueError) as ctx:
            svc.resolve_playlist("nothing like this")
        self.assertIn("Paste its link", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            svc.resolve_playlist(ID)
        self.assertIn("would not give me", str(ctx.exception))

    def test_followed_playlists_are_added_after_the_prefixed_ones_under_the_chosen_name(self):
        app_config.add_extra_playlist(ID, "the-paris-drive", "Paris Drive")
        svc = service(FakeSp(library=[lib("A" * 22, "FF Dub", "Me")],
                             readable={ID: {"id": ID, "name": "the-paris-drive", "snapshot_id": "s9", "owner": {"display_name": "Zayan Khan"}}}))
        playlists = svc.get_all_playlists()
        self.assertEqual([(p["display_name"], p["id"]) for p in playlists], [("Dub", "A" * 22), ("Paris Drive", ID)])
        self.assertEqual(svc.skipped, [])

    def test_a_followed_playlist_spotify_will_not_give_is_skipped_and_reported(self):
        app_config.add_extra_playlist(ID, "editorial thing", "Editorial")
        svc = service(FakeSp(library=[lib("A" * 22, "FF Dub", "Me")]))
        playlists = svc.get_all_playlists()
        self.assertEqual([p["display_name"] for p in playlists], ["Dub"], "the sync carries on without it")
        self.assertEqual([name for name, _ in svc.skipped], ["Editorial"])

    def test_somebody_elses_playlist_whose_tracks_are_blocked_cannot_be_followed(self):
        svc = service(FakeSp(library=[lib(ID, "the-paris-drive")], tracks_blocked={ID}))
        with self.assertRaises(ValueError) as ctx:
            svc.resolve_followable("the-paris-drive")
        self.assertIn("only lets sff read the tracks of playlists you own", str(ctx.exception))
        self.assertIn("Zayan Khan", str(ctx.exception))

    def test_a_readable_playlist_can_be_followed(self):
        svc = service(FakeSp(library=[lib(ID, "mine too", "Me")]))
        self.assertEqual(svc.resolve_followable("mine too")["id"], ID)

    def test_a_followed_playlist_that_became_unreadable_is_skipped_with_the_reason_not_synced(self):
        app_config.add_extra_playlist(ID, "the-paris-drive")
        svc = service(FakeSp(readable={ID: {"id": ID, "name": "the-paris-drive", "snapshot_id": "s", "owner": {"display_name": "Z"}}},
                             tracks_blocked={ID}))
        self.assertEqual(svc.get_all_playlists(), [])
        self.assertIn("only lets sff read the tracks", svc.skipped[0][1])

    def test_following_a_playlist_that_already_has_the_prefix_does_not_sync_it_twice(self):
        app_config.add_extra_playlist("A" * 22, "FF Dub")
        svc = service(FakeSp(library=[lib("A" * 22, "FF Dub", "Me")]))
        self.assertEqual(len(svc.get_all_playlists()), 1)


class FollowedPlaylistSyncTest(SyncFixture):
    """A followed playlist goes through the whole pipeline, and what its owner removes is removed here too."""

    def setUp(self):
        super().setUp()
        outer = self
        self.paris = [spotify_track(0, "id-p1", "Paris Song One", "Artist P", 0),
                      spotify_track(0, "id-p2", "Paris Song Two", "Artist Q", 1)]
        self.skipped = [("Broken Link", "403 Forbidden")]

        class FakeSpotifyWithFollowed:
            skipped = outer.skipped

            def get_all_playlists(self):
                return [{"id": "PARIS", "name": "the-paris-drive", "display_name": "the-paris-drive", "snapshot_id": "s"}]

            def get_playlist_tracks(self, pid, name):
                return [spotify_track(0, t.spotify_id, t.title, t.artist, i) for i, t in enumerate(outer.paris)]

        mock.patch("services.spotify.SpotifyService", FakeSpotifyWithFollowed).start()
        from services import app_config as cfg
        cfg.set_output_formats(["aiff"])
        self.nico.offer("Artist P", "Paris Song One", "flac16", "u1", r"m\01 - Paris Song One.flac", {"4": 44100, "5": 16})
        self.nico.offer("Artist Q", "Paris Song Two", "flac16", "u2", r"m\02 - Paris Song Two.flac", {"4": 44100, "5": 16})

    def test_the_followed_playlist_becomes_a_rekordbox_playlist_and_a_bad_link_is_reported_not_fatal(self):
        result = self.run_sync()
        names = [p["name"] for p in self.rbfake.playlists.values()]
        self.assertEqual(names, ["the-paris-drive AIFF"])
        titles = [self.rbfake.contents[c]["title"] for c in next(iter(self.rbfake.playlists.values()))["tracks"]]
        self.assertEqual(sorted(titles), ["Paris Song One", "Paris Song Two"])
        self.assertTrue(any("Broken Link" in e for e in result["errors"]), result["errors"])

    def test_a_track_its_owner_removes_leaves_the_playlist_but_not_the_disk(self):
        self.run_sync()
        self.paris = [t for t in self.paris if t.title != "Paris Song Two"]
        self.run_sync()
        self.assertEqual([name for name, _ in self.rbfake.removed], ["the-paris-drive AIFF"])
        self.assertTrue(any("Paris Song Two" in f.name for f in self.files("the-paris-drive AIFF", "aiff")), "file kept on disk")

    def test_a_track_its_owner_adds_is_fetched_on_the_next_sync(self):
        self.run_sync()
        self.paris.append(spotify_track(0, "id-p3", "Paris Song Three", "Artist R", 2))
        self.nico.offer("Artist R", "Paris Song Three", "flac16", "u3", r"m\03 - Paris Song Three.flac", {"4": 44100, "5": 16})
        self.run_sync()
        self.assertIn(soulseek._build_query("Artist R", "Paris Song Three"), self.nico.searches)
        titles = [self.rbfake.contents[c]["title"] for c in next(iter(self.rbfake.playlists.values()))["tracks"]]
        self.assertIn("Paris Song Three", titles)


if __name__ == "__main__":
    unittest.main()
