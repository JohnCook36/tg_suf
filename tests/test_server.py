import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer

from tg_suf.models import NewsPost
from tg_suf.server import ReaderHandler
from tg_suf.storage import Store


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = os.environ.get("TG_SUF_DATA_DIR")
        os.environ["TG_SUF_DATA_DIR"] = self.tmp.name
        with Store(Path(self.tmp.name) / "reader.sqlite3") as db:
            db.create_queue([(1, "Тест", [NewsPost(1, 1, "Тест", "Привет", "2026-10-24")])])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ReaderHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.old_path is None:
            os.environ.pop("TG_SUF_DATA_DIR", None)
        else:
            os.environ["TG_SUF_DATA_DIR"] = self.old_path
        self.tmp.cleanup()

    def request(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        h = {"Host": "127.0.0.1:8765", **(headers or {})}
        if data is not None:
            h["Content-Type"] = "application/json"
            data = json.dumps(data).encode("utf-8")
        conn.request(method, path, data, h)
        resp = conn.getresponse()
        code = resp.status
        body = resp.read()
        conn.close()
        return code, json.loads(body) if resp.getheader("Content-Type", "").startswith("application/json") else body

    def test_api_checkpoint_and_conflict(self):
        code, state = self.request("GET", "/api/state")
        self.assertEqual(code, 200)
        cp = state["progress"]
        payload = {"run_id": cp["run_id"], "ordinal": 0, "char_offset": 3,
                   "action": "pause", "expected_revision": cp["revision"]}
        code, state = self.request("POST", "/api/checkpoint", payload)
        self.assertEqual(code, 200)
        self.assertEqual(state["progress"]["char_offset"], 3)
        self.assertEqual(self.request("POST", "/api/checkpoint", payload)[0], 409)

    def test_host_origin_restrictions(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        cp = self.request("GET", "/api/state")[1]["progress"]
        body = {"run_id": cp["run_id"], "ordinal": 0, "char_offset": 2,
                "action": "pause", "expected_revision": cp["revision"]}
        self.assertEqual(self.request("POST", "/api/checkpoint", body,
                                      headers={"Origin": "https://evil.example"})[0], 403)

    def test_reject_boolean_as_number(self):
        cp = self.request("GET", "/api/state")[1]["progress"]
        body = {"run_id": True, "ordinal": 0, "char_offset": 2,
                "action": "pause", "expected_revision": cp["revision"]}
        self.assertEqual(self.request("POST", "/api/checkpoint", body)[0], 400)


if __name__ == "__main__":
    unittest.main()
