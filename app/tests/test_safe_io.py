import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from services import safe_io, soulseek


class AtomicWriteTests(unittest.TestCase):
    def test_failed_write_keeps_the_old_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "state.json"
            f.write_text('{"ok": 1}', encoding="utf-8")
            with mock.patch.object(Path, "write_text", side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(OSError):
                    safe_io.write_text_atomic(f, json.dumps({"new": 1}))
            self.assertEqual(json.loads(f.read_text(encoding="utf-8")), {"ok": 1})
            self.assertFalse((Path(d) / "state.json.tmp").exists())

    def test_normal_write_replaces_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "state.json"
            safe_io.write_text_atomic(f, "one")
            safe_io.write_text_atomic(f, "two")
            self.assertEqual(f.read_text(encoding="utf-8"), "two")


class WaitForSpaceTests(unittest.TestCase):
    def test_waits_until_space_returns(self):
        readings = iter([3.0, 4.0, 40.0])
        naps = []
        with mock.patch.object(safe_io, "free_gb", side_effect=lambda p: next(readings)):
            ok = safe_io.wait_for_space("D:/x", min_free_gb=15, sleep=naps.append)
        self.assertTrue(ok)
        self.assertEqual(len(naps), 2)

    def test_no_wait_when_there_is_room(self):
        with mock.patch.object(safe_io, "free_gb", return_value=100.0):
            self.assertTrue(safe_io.wait_for_space("D:/x", sleep=lambda s: self.fail("slept")))


class GoneSearchTests(unittest.TestCase):
    def test_a_400_for_an_unknown_search_is_no_results(self):
        err = urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
        with mock.patch.object(soulseek, "_api_get", side_effect=err):
            self.assertEqual(soulseek._fetch_all_results(5), [])

    def test_other_http_errors_still_raise(self):
        err = urllib.error.HTTPError("u", 500, "boom", {}, None)
        with mock.patch.object(soulseek, "_api_get", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                soulseek._fetch_all_results(5)


if __name__ == "__main__":
    unittest.main()
