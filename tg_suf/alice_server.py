"""Isolated Alice Dialogs webhook. Never expose the unauthenticated browser server.

The HTTPS reverse proxy should route a long random path secret to this
loopback-only server. Alice's authenticated user id is ALSO allowlisted.
"""
from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from .alice_skill import handle_alice
from .config import data_dir, load_dotenv
from .storage import ConflictError, Store


class AliceHandler(BaseHTTPRequestHandler):
    def send_json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.send_json(405, {"error": "post_required"})

    def do_POST(self) -> None:
        load_dotenv()
        secret = os.environ.get("ALICE_WEBHOOK_SECRET", "").strip()
        allowed_user = os.environ.get("ALICE_ALLOWED_USER_ID", "").strip()
        allowed_app = os.environ.get("ALICE_ALLOWED_APPLICATION_ID", "").strip()
        # Misconfiguration must NEVER expose the user's private Telegram posts.
        if len(secret) < 32 or not (allowed_user or allowed_app):
            return self.send_json(503, {"error": "alice_not_configured"})
        if not hmac.compare_digest(
            urlsplit(self.path).path, "/alice/" + secret
        ):
            return self.send_json(404, {"error": "not_found"})
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.send_json(415, {"error": "json_required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                return self.send_json(413, {"error": "invalid_size"})
            payload = json.loads(self.rfile.read(length))
            with Store(data_dir() / "reader.sqlite3") as db:
                reply = handle_alice(
                    payload, db,
                    allowed_user=allowed_user,
                    allowed_application=allowed_app,
                )
            return self.send_json(200, reply)
        except PermissionError:
            return self.send_json(403, {"error": "forbidden"})
        except (ValueError, TypeError, json.JSONDecodeError):
            return self.send_json(400, {"error": "invalid_request"})
        except ConflictError:
            return self.send_json(409, {"error": "progress_conflict"})
        except Exception:
            # Do not dump Telegram post text or tokens in error responses/logs.
            return self.send_json(500, {"error": "internal_error"})

    def log_message(self, format: str, *args: object) -> None:
        pass  # URL contains a secret; never log it.


def serve_alice(host: str = "127.0.0.1", port: int = 8766) -> None:
    load_dotenv()
    secret = os.environ.get("ALICE_WEBHOOK_SECRET", "").strip()
    if len(secret) < 32:
        raise ValueError("ALICE_WEBHOOK_SECRET must contain at least 32 characters")
    if not (os.environ.get("ALICE_ALLOWED_USER_ID", "").strip()
            or os.environ.get("ALICE_ALLOWED_APPLICATION_ID", "").strip()):
        raise ValueError("Set ALICE_ALLOWED_USER_ID (preferred) or ALICE_ALLOWED_APPLICATION_ID")
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Alice server must be loopback-only behind a TLS reverse proxy")
    with HTTPServer((host, port), AliceHandler) as server:
        print(f"Приватный навык Алисы слушает {host}:{port}. Нужен HTTPS-прокси.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
