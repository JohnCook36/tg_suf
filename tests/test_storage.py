import tempfile
import unittest
from pathlib import Path

from tg_suf.models import NewsPost
from tg_suf.storage import Store, ConflictError, ActiveQueueError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def posts(self):
        return [
            (10, "А", [
                NewsPost(10, 3, "А", "Третья", "2026-10-24T11:00:00Z"),
                NewsPost(10, 1, "А", "Первая", "2026-10-24T09:00:00Z"),
                NewsPost(10, 3, "А", "Дублированный ID", "2026-10-24T11:00:00Z"),
            ]),
            (20, "Б", [NewsPost(20, 3, "Б", "Другой канал", "2026-10-24T08:00:00Z")]),
        ]

    def test_order_and_unique_per_channel(self):
        with Store(self.path) as db:
            created = db.create_queue(self.posts())
            self.assertEqual(created["count"], 3)
            ids = [(i["peer_id"], i["message_id"]) for i in db.items(created["run_id"])]
            self.assertEqual(ids, [(10, 1), (10, 3), (20, 3)])

    def test_pause_resume_and_finished_status(self):
        with Store(self.path) as db:
            db.create_queue(self.posts())
            cp = db.current()
            db.save(run_id=cp["run_id"], ordinal=0, char_offset=3, action="pause", expected_revision=cp["revision"])
        with Store(self.path) as db:
            cp = db.current()
            self.assertEqual((cp["ordinal"], cp["char_offset"]), (0, 3))
            done = db.save(run_id=cp["run_id"], ordinal=0, char_offset=6, action="complete", expected_revision=cp["revision"])
            self.assertEqual((done["progress"]["ordinal"], done["progress"]["char_offset"]), (1, 0))

    def test_optimistic_lock(self):
        with Store(self.path) as db:
            db.create_queue(self.posts())
            cp = db.current()
            db.save(run_id=cp["run_id"], ordinal=0, char_offset=2, action="playing", expected_revision=cp["revision"])
            with self.assertRaises(ConflictError):
                db.save(run_id=cp["run_id"], ordinal=0, char_offset=1, action="pause", expected_revision=cp["revision"])

    def test_active_queue_protected(self):
        with Store(self.path) as db:
            first = db.create_queue(self.posts())
            with self.assertRaises(ActiveQueueError):
                db.create_queue(self.posts())
            new = db.create_queue(self.posts(), replace=True)
            self.assertNotEqual(first["run_id"], new["run_id"])

    def test_empty_and_wrong_channel(self):
        with Store(self.path) as db:
            with self.assertRaises(ValueError):
                db.create_queue([(42, "Пустой", [])])
            with self.assertRaises(ValueError):
                db.create_queue([(42, "Не тот", [NewsPost(10, 1, "X", "Текст", "2026-10-24")])])

    def test_invalid_offset_and_action(self):
        with Store(self.path) as db:
            db.create_queue(self.posts())
            cp = db.current()
            for action, position in [("pause", 500), ("invalid", 0)]:
                with self.assertRaises(ValueError):
                    db.save(run_id=cp["run_id"], ordinal=0, char_offset=position,
                            action=action, expected_revision=cp["revision"])
            self.assertEqual(db.current()["revision"], cp["revision"])

    def test_end_of_queue(self):
        with Store(self.path) as db:
            db.create_queue([(4, "Один", [NewsPost(4, 1, "Один", "Текст", "2026-10-24")])])
            cp = db.current()
            finished = db.save(run_id=cp["run_id"], ordinal=0, char_offset=0,
                               action="skip", expected_revision=cp["revision"])
            self.assertEqual(finished["progress"]["ordinal"], 1)
            with self.assertRaises(ConflictError):
                db.save(run_id=cp["run_id"], ordinal=1, char_offset=0,
                        action="complete", expected_revision=finished["progress"]["revision"])


if __name__ == "__main__":
    unittest.main()
