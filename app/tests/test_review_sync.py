import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import review
import review_sync
from services import audit_log


class Machine:
    """One computer's review state, kept in its own temp folder."""

    def __init__(self, name: str, tmp: Path):
        self.root = tmp / name / "Music" / "Incoming"
        self.audit = tmp / name / "audit.csv"
        self.marks = tmp / name / "marks.json"
        self.root.mkdir(parents=True)

    def add_flagged_file(self, rel: str, title: str, mark: dict | None = None):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        with self.use():
            audit_log.append([{"playlist": "p", "title": title, "verdict": "check", "similarity_to_reference": "0.9",
                               "bass_share": "0.4", "new_file": str(path), "reference": ""}])
            if mark:
                marks = review.load_marks()
                marks[review._item_id(str(path))] = mark
                review._save_marks(marks)
        return path

    def use(self):
        stack = mock.patch.multiple(review, AUDIT_FILE=self.audit, MARKS_FILE=self.marks, HISTORY_FILE=self.marks.with_name("h.jsonl"))
        stack2 = mock.patch.object(audit_log, "FILE", self.audit)

        class Both:
            def __enter__(s):
                stack.__enter__(); stack2.__enter__()

            def __exit__(s, *a):
                stack2.__exit__(*a); stack.__exit__(*a)
        return Both()

    def marks_by_rel(self):
        with self.use():
            data = review.load_marks()
        out = {}
        for iid, m in data.items():
            out[iid] = m
        return out


class ReviewSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pc, self.mac = Machine("pc", self.tmp), Machine("mac", self.tmp)

    def bundle(self):
        with self.pc.use():
            return review_sync.build_bundle(str(self.pc.root))

    def test_marks_labels_and_notes_are_keyed_by_the_path_inside_the_music_folder(self):
        self.pc.add_flagged_file("Dub AIFF/a.aiff", "A", {"mark": "right", "label": "Dub", "note": "sax solo"})
        b = self.bundle()
        self.assertEqual(b["marks"], {"Dub AIFF/a.aiff": {"mark": "right", "label": "Dub", "note": "sax solo"}})
        self.assertEqual(b["audit"][0]["new_file"], "Dub AIFF/a.aiff", "relative, so it means the same on the other computer")

    def test_a_file_that_was_set_aside_or_is_gone_is_left_out(self):
        self.pc.add_flagged_file("Dub AIFF/a.aiff", "A", {"mark": "wrong", "rejected": True})
        gone = self.pc.add_flagged_file("Dub AIFF/b.aiff", "B", {"mark": "right"})
        gone.unlink()
        b = self.bundle()
        self.assertEqual((b["audit"], b["marks"], b["skipped"]), ([], {}, 2))

    def test_the_other_computer_gets_the_item_with_its_own_paths_and_the_note(self):
        self.pc.add_flagged_file("Dub AIFF/a.aiff", "A", {"mark": "right", "note": "Bangkok Groove"})
        (self.mac.root / "Dub AIFF").mkdir(parents=True)
        (self.mac.root / "Dub AIFF" / "a.aiff").write_bytes(b"audio")
        with self.mac.use():
            result = review_sync.apply_bundle(self.bundle(), str(self.mac.root))
            items = review.load_items()
        self.assertEqual((result["queued"], result["marks_added"], result["conflicts"]), (1, 1, []))
        self.assertEqual([(i["title"], i["mark"], i["note"]) for i in items], [("A", "right", "Bangkok Groove")])

    def test_a_clash_keeps_the_local_decision_and_reports_it(self):
        self.pc.add_flagged_file("Dub AIFF/a.aiff", "A", {"mark": "right", "label": "Dub"})
        self.mac.add_flagged_file("Dub AIFF/a.aiff", "A", {"mark": "wrong"})
        with self.mac.use():
            result = review_sync.apply_bundle(self.bundle(), str(self.mac.root))
            local = list(review.load_marks().values())[0]
        self.assertEqual(local["mark"], "wrong", "the local mark stays")
        self.assertEqual(local["label"], "Dub", "a field that was empty here is filled in")
        self.assertEqual([(c["field"], c["kept_here"], c["other"]) for c in result["conflicts"]], [("mark", "wrong", "right")])

    def test_merging_twice_changes_nothing_the_second_time(self):
        self.pc.add_flagged_file("Dub AIFF/a.aiff", "A", {"mark": "right", "note": "n"})
        (self.mac.root / "Dub AIFF").mkdir(parents=True)
        (self.mac.root / "Dub AIFF" / "a.aiff").write_bytes(b"audio")
        with self.mac.use():
            review_sync.apply_bundle(self.bundle(), str(self.mac.root))
            again = review_sync.apply_bundle(self.bundle(), str(self.mac.root))
        self.assertEqual((again["queued"], again["marks_added"], again["marks_filled"], again["conflicts"]), (0, 0, 0, []))

    def test_a_dry_run_writes_nothing(self):
        self.pc.add_flagged_file("Dub AIFF/a.aiff", "A", {"mark": "right"})
        (self.mac.root / "Dub AIFF").mkdir(parents=True)
        (self.mac.root / "Dub AIFF" / "a.aiff").write_bytes(b"audio")
        with self.mac.use():
            result = review_sync.apply_bundle(self.bundle(), str(self.mac.root), write=False)
        self.assertEqual(result["queued"], 1)
        self.assertFalse(self.mac.audit.exists() or self.mac.marks.exists())

    def test_an_item_whose_file_is_not_on_this_computer_is_not_queued(self):
        self.pc.add_flagged_file("Dub AIFF/a.aiff", "A", {"mark": "right"})
        with self.mac.use():
            result = review_sync.apply_bundle(self.bundle(), str(self.mac.root))
        self.assertEqual(result["queued"], 0)


if __name__ == "__main__":
    unittest.main()
