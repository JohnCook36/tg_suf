import tempfile
import unittest
from pathlib import Path

from tg_suf.alice_skill import CHUNK_LIMIT, handle_alice
from tg_suf.models import NewsPost
from tg_suf.storage import Store


USER = "private-user-123"


def command(text="", *, message_id=0, session_id="session-1", user_id=USER):
    return {
        "version": "1.0",
        "session": {
            "skill_id": "skill-1",
            "session_id": session_id,
            "message_id": message_id,
            "new": message_id == 0,
            "user": {"user_id": user_id},
            "application": {"application_id": "app-1"},
        },
        "request": {"type": "SimpleUtterance", "command": text},
    }


class AliceSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.tmp.name) / "reader.sqlite3")
        self.db.create_queue([
            (11, "Технологии", [
                NewsPost(11, 1, "Технологии", "Первое предложение. " * 100,
                         "2026-10-26T09:00:00Z"),
            ]),
            (22, "Новости", [
                NewsPost(22, 1, "Новости", "Вторая новость.", "2026-10-26T10:00:00Z"),
            ]),
        ])

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def say(self, text, message_id, session_id="session-1"):
        return handle_alice(
            command(text, message_id=message_id, session_id=session_id),
            self.db, allowed_user=USER,
        )["response"]

    def test_initial_read_is_conservative_and_short(self):
        resp = self.say("", 0)
        self.assertIn("Канал Технологии.", resp["tts"])
        self.assertLessEqual(len(resp["tts"]), 1024)
        self.assertEqual(self.db.current()["char_offset"], 0)
        self.assertEqual(self.db.current()["ordinal"], 0)

    def test_deduplicated_next_and_restart(self):
        self.say("читай", 0)
        first = self.say("дальше", 1)
        cp = self.db.current()
        self.assertGreater(cp["char_offset"], 0)
        self.assertEqual(cp["ordinal"], 0)
        self.assertEqual(first, self.say("дальше", 1))
        self.assertEqual(self.db.current()["revision"], cp["revision"])
        self.say("пауза", 2)
        self.assertEqual(self.db.current()["char_offset"], cp["char_offset"])
        resumed = self.say("продолжи", 0, "session-2")
        self.assertIn("Первое предложение", resumed["tts"])
        self.assertEqual(self.db.current()["char_offset"], cp["char_offset"])

    def test_news_advance_requires_acknowledgement(self):
        self.say("читай", 0)
        counter = 1
        while self.db.current()["ordinal"] == 0:
            self.say("дальше", counter)
            counter += 1
            self.assertLess(counter, 20)
        self.assertEqual(self.db.current()["ordinal"], 1)
        self.assertIn("Вторая новость", self.say("читай", counter)["text"])
        self.say("дальше", counter + 1)
        self.assertEqual(self.db.current()["ordinal"], 2)

    def test_pause_and_skip(self):
        self.say("читай", 0)
        paused = self.say("останови", 1)
        self.assertIn("Остановлено", paused["text"])
        self.assertEqual(self.db.current()["ordinal"], 0)
        self.assertEqual(self.db.current()["char_offset"], 0)
        self.assertIn("Вторая новость", self.say("пропусти новость", 2)["text"])
        self.assertEqual(self.db.current()["ordinal"], 1)

    def test_access_control(self):
        with self.assertRaises(PermissionError):
            handle_alice(command(user_id="not-me"), self.db, allowed_user=USER)
        with self.assertRaises(PermissionError):
            handle_alice(command(), self.db)
        with self.assertRaises(PermissionError):
            handle_alice(command(), self.db, allowed_application="wrong-app")
        accepted = handle_alice(command(), self.db, allowed_application="app-1")
        self.assertIn("tts", accepted["response"])

    def test_reject_invalid_message_id(self):
        payload = command()
        payload["session"]["message_id"] = True
        with self.assertRaises(ValueError):
            handle_alice(payload, self.db, allowed_user=USER)

    def test_sanitize_telegram_markup(self):
        self.db.create_queue([(
            100, "Тест", [NewsPost(100, 3, "Тест",
                                "<speaker audio='evil'>Опасный тег</speaker>.",
                                "2026-10-26")],
        )], replace=True)
        spoken = self.say("читай", 0, "new-session")
        self.assertNotIn("<", spoken["tts"])
        self.assertNotIn(">", spoken["tts"])

    def test_stale_pending_does_not_overwrite_browser_progress(self):
        self.say("читай", 0)
        cp = self.db.current()
        self.db.save(
            run_id=cp["run_id"], ordinal=cp["ordinal"],
            char_offset=5, action="pause", expected_revision=cp["revision"],
        )
        answer = self.say("дальше", 1)
        self.assertIn("Сначала скажи читай", answer["text"])
        self.assertEqual(self.db.current()["char_offset"], 5)


if __name__ == "__main__":
    unittest.main()
