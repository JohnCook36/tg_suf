import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path

from tg_suf.alice_server import AliceHandler
from tg_suf.models import NewsPost
from tg_suf.storage import Store


SECRET = "abcdefghijklmnopqrstuv0123456789abcdefgh"


class AliceHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vars = {}
        for key, val in {
            "TG_SUF_DATA_DIR": self.tmp.name,
            "ALICE_WEBHOOK_SECRET": SECRET,
            "ALICE_ALLOWED_USER_ID": "alice-user",
            "ALICE_ALLOWED_APPLICATION_ID": "",
        }.items():
            self.vars[key] = os.environ.get(key)
            os.environ[key] = val
        with Store(Path(self.tmp.name) / "reader.sqlite3") as db:
            db.create_queue([(8, "Канал", [
                NewsPost(8, 1, "Канал", "Тестовая публикация", "2026-10-26")
            ])])
        self.server = HTTPServer(("127.0.0.1", 0), AliceHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        for key, previous in self.vars.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        self.tmp.cleanup()

    def request(self, path, *, user="alice-user"):
        payload = {
            "version": "1.0",
            "request": {"type": "SimpleUtterance", "command": "читай"},
            "session": {
                "session_id": "sess", "skill_id": "skill", "message_id": 0,
                "user": {"user_id": user}, "application": {"application_id": "app"}
            },
        }
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        conn.request("POST", path, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        response = conn.getresponse()
        code = response.status
        body = json.loads(response.read().decode())
        conn.close()
        return code, body

    def test_authenticated_endpoint(self):
        code, body = self.request("/alice/" + SECRET)
        self.assertEqual(code, 200)
        self.assertIn("Тестовая публикация", body["response"]["tts"])
        self.assertEqual(body["version"], "1.0")

    def test_bad_secret_and_wrong_user(self):
        code, body = self.request("/alice/" + "z" * len(SECRET))
        self.assertEqual(code, 404)
        self.assertNotIn("Тестовая публикация", str(body))
        code, body = self.request("/alice/" + SECRET, user="stranger")
        self.assertEqual(code, 403)
        self.assertNotIn("Тестовая публикация", str(body))

    def test_duplicate_request_is_not_advanced(self):
        path = "/alice/" + SECRET
        code, first = self.request(path)
        code, again = self.request(path)
        self.assertEqual(first, again)
        with Store(Path(self.tmp.name) / "reader.sqlite3") as db:
            self.assertEqual(db.current()["ordinal"], 0)
            self.assertEqual(db.current()["char_offset"], 0)


if __name__ == "__main__":
    unittest.main()
