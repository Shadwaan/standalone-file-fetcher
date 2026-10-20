import unittest

from services import to_buy


def failure(**kw):
    base = {"spotify_id": "a", "artist": "Some Artist, Other", "title": "Some Song", "playlist": "Dub AIFF", "category": "no results",
            "detail": "the network returned no results for it", "times_failed": 3, "last_failed": "2026-10-20T04:00:00"}
    base.update(kw)
    return base


class ToBuyTest(unittest.TestCase):
    def test_a_track_never_found_in_three_runs_is_not_on_soulseek(self):
        (row,) = to_buy.build_list({}, [failure()])
        self.assertEqual((row["group"], row["runs_failed"], row["last_tried"]), (to_buy.NOT_ON_SOULSEEK, 3, "2026-10-20"))

    def test_tried_once_or_twice_is_not_called_certain(self):
        (row,) = to_buy.build_list({}, [failure(times_failed=1)])
        self.assertEqual(row["group"], to_buy.UNSURE)

    def test_only_bad_copies_over_three_runs_is_its_own_group(self):
        (row,) = to_buy.build_list({}, [failure(category="wrong quality / dead source", detail="2 matching files were found but none was usable")])
        self.assertEqual(row["group"], to_buy.BAD_COPIES_ONLY)

    def test_the_most_certain_come_first_then_by_playlist(self):
        rows = to_buy.build_list({}, [failure(spotify_id="u", times_failed=1, title="Unsure"),
                                      failure(spotify_id="z", playlist="Z AIFF", title="Z"),
                                      failure(spotify_id="a", playlist="A AIFF", title="A")])
        self.assertEqual([r["title"] for r in rows], ["A", "Z", "Unsure"])

    def test_a_youtube_stand_in_is_shown_so_it_is_known_what_is_in_the_playlist(self):
        state = {"playlists": {"p": {"variants": {"aiff": {"display_name": "Dub AIFF", "tracks": {
            "a": {"stand_in": True, "stand_in_label": "YT opus 130k", "source": {"how": "youtube"}},
            "b": {"stand_in": True, "stand_in_label": "Dub", "source": {"how": "downloaded"}}}}}}}}
        rows = to_buy.build_list(state, [failure(spotify_id="a"), failure(spotify_id="b", title="Other")])
        self.assertEqual({r["title"]: r["youtube_copy"] for r in rows}, {"Some Song": "YT opus 130k", "Other": ""})

    def test_links_search_the_first_artist_and_the_title(self):
        l = to_buy.links("Some Artist, Other", "Some Song (Dub)")
        self.assertIn("q=Some+Artist+Some+Song+%28Dub%29", l["bandcamp"])
        self.assertTrue(l["beatport"].startswith("https://www.beatport.com/search?q="))

    def test_the_csv_has_a_header_and_one_row_per_track(self):
        text = to_buy.as_csv(to_buy.build_list({}, [failure()]))
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("playlist,artist,title"))


if __name__ == "__main__":
    unittest.main()
