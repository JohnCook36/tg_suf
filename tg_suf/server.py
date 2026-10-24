"""Loopback-only HTTP server for local browser speech demo.

Do NOT expose on the network. This stage does not include API authentication.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit

from .config import data_dir
from .storage import ConflictError, Store

HOST, PORT = "127.0.0.1", 8765


class ReaderHandler(BaseHTTPRequestHandler):
    def _send(self, code, payload, mimetype):
        self.send_response(code)
        self.send_header("Content-Type", mimetype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _allowed_host(self):
        return self.headers.get("Host") in {f"{HOST}:{PORT}", f"localhost:{PORT}"}

    def do_GET(self):
        if not self._allowed_host():
            return self._json(403, {"error": "invalid_host"})
        path = urlsplit(self.path).path
        if path == "/api/state":
            with Store(data_dir() / "reader.sqlite3") as db:
                return self._json(200, db.state())
        resources = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        if path not in resources:
            return self._json(404, {"error": "not_found"})
        filename, mimetype = resources[path]
        return self._send(200, files("tg_suf").joinpath("web", filename).read_bytes(), mimetype)

    def do_POST(self):
        if not self._allowed_host():
            return self._json(403, {"error": "invalid_host"})
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}:
            return self._json(403, {"error": "invalid_origin"})
        if urlsplit(self.path).path != "/api/checkpoint":
            return self._json(404, {"error": "not_found"})
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._json(415, {"error": "json_required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                return self._json(413, {"error": "invalid_size"})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
            args = {name: data[name] for name in ("run_id", "ordinal", "char_offset", "action", "expected_revision")}
            if any(type(args[name]) is not int for name in ("run_id", "ordinal", "char_offset", "expected_revision")):
                raise ValueError("Numeric fields required")
            if not isinstance(args["action"], str):
                raise ValueError("Action must be string")
            with Store(data_dir() / "reader.sqlite3") as db:
                return self._json(200, db.save(**args))
        except (ValueError, KeyError, TypeError) as exc:
            return self._json(400, {"error": "invalid_request", "detail": str(exc)})
        except ConflictError as exc:
            return self._json(409, {"error": "checkpoint_conflict", "detail": str(exc)})

    def log_message(self, format, *args):
        pass  # Avoid logging content and user URLs.


def serve(*, open_browser=True):
    import webbrowser
    with ThreadingHTTPServer((HOST, PORT), ReaderHandler) as server:
        address = f"http://{HOST}:{PORT}"
        print(f"Ручной диктор: {address} (Ctrl+C для выхода)")
        if open_browser:
            webbrowser.open(address)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Плеер остановлен.")
