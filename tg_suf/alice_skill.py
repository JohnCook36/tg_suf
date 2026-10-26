"""Private Yandex Alice dialog prototype over the existing, frozen SQLite queue.

Alice Dialogs returns short TTS responses; it does not report when speech finished.
Consequently progress advances ONLY after an explicit "дальше" acknowledgement.
"""
from __future__ import annotations

import json
import re
from sqlite3 import Connection

from .storage import ConflictError, Store

CHUNK_LIMIT = 760


def _speakable(text: str) -> str:
    """Prevent Telegram post text from injecting Alice speaker markup."""
    return re.sub(r"[<>]", " ", text).replace("\x00", " ").strip()


def _chunk(text: str, start: int, budget: int) -> tuple[str, int]:
    remaining = text[start:]
    if len(remaining) <= budget:
        return remaining, len(text)
    candidate = remaining[:budget]
    # Prefer a sentence boundary; do not create tiny fragments.
    sentence = max(candidate.rfind(mark) for mark in ".!?\n")
    word = candidate.rfind(" ")
    split = sentence + 1 if sentence >= budget // 2 else (word if word >= budget // 2 else budget)
    return remaining[:split], start + split


def _response(text: str, *, end: bool = False, tts: str | None = None) -> dict:
    rendered = _speakable(text)[:1024]
    return {
        "version": "1.0",
        "response": {
            "text": rendered,
            "tts": _speakable(tts if tts is not None else text)[:1024],
            "end_session": end,
        },
    }


def _schema(db: Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS alice_pending (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            run_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alice_replies (
            request_key TEXT PRIMARY KEY,
            reply_json TEXT NOT NULL
        );
    """)


def _identity(payload: dict, allowed_user: str, allowed_application: str) -> str:
    session = payload.get("session")
    if not isinstance(session, dict):
        raise PermissionError("Missing session")
    user = session.get("user") or {}
    app = session.get("application") or {}
    if not isinstance(user, dict) or not isinstance(app, dict):
        raise PermissionError("Invalid identity")
    if allowed_user:
        if not isinstance(user.get("user_id"), str) or user["user_id"] != allowed_user:
            raise PermissionError("Unknown Alice user")
        return "user:" + allowed_user
    if allowed_application:
        if not isinstance(app.get("application_id"), str) or app["application_id"] != allowed_application:
            raise PermissionError("Unknown Alice application")
        return "application:" + allowed_application
    raise PermissionError("Set ALICE_ALLOWED_USER_ID or ALICE_ALLOWED_APPLICATION_ID")


def _request_key(payload: dict, identity: str) -> str:
    session = payload["session"]
    message_id = session.get("message_id")
    session_id = session.get("session_id")
    skill_id = session.get("skill_id")
    if type(message_id) is not int or message_id < 0:
        raise ValueError("Invalid message id")
    if not isinstance(session_id, str) or not session_id or not isinstance(skill_id, str) or not skill_id:
        raise ValueError("Missing session or skill id")
    return json.dumps([identity, skill_id, session_id, message_id], ensure_ascii=False)


def _current(db: Store) -> dict:
    return db.state()


def _item(state: dict) -> dict | None:
    cp = state["progress"]
    if cp is None or cp["ordinal"] >= cp["total"]:
        return None
    return state["items"][cp["ordinal"]]


def _pending(db: Store):
    return db.connection.execute("SELECT * FROM alice_pending WHERE singleton=1").fetchone()


def _clear_pending(db: Store) -> None:
    with db.connection:
        db.connection.execute("DELETE FROM alice_pending WHERE singleton=1")


def _read(db: Store) -> dict:
    state = _current(db)
    cp = state["progress"]
    item = _item(state)
    if not item:
        return _response("Очередь завершена. Добавь новые новости командой синхронизации.")
    start = cp["char_offset"]
    text = item["frozen_text"]
    if start >= len(text):
        # Defensive recovery of an incomplete checkpoint.
        return _response("Новости почти закончились. Скажи дальше.")
    title = _speakable(item["frozen_title"])[:80]
    intro = f"Канал {title}. " if start == 0 else ""
    text_chunk, end = _chunk(text, start, CHUNK_LIMIT - len(intro))
    speech = (intro + _speakable(text_chunk)).strip()
    with db.connection:
        db.connection.execute("""
            INSERT INTO alice_pending (singleton, run_id, ordinal, start_offset, end_offset)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                run_id=excluded.run_id, ordinal=excluded.ordinal,
                start_offset=excluded.start_offset, end_offset=excluded.end_offset
        """, (cp["run_id"], cp["ordinal"], start, end))
    return _response(speech)


def _next(db: Store) -> dict:
    state = _current(db)
    cp = state["progress"]
    item = _item(state)
    if not item:
        return _response("Очередь закончилась.")
    pending = _pending(db)
    if pending is None or (pending["run_id"], pending["ordinal"], pending["start_offset"]) != (
        cp["run_id"], cp["ordinal"], cp["char_offset"]
    ):
        return _response("Чтобы начать чтение, скажи: читай.")
    end_offset = pending["end_offset"]
    action = "complete" if end_offset >= len(item["frozen_text"]) else "pause"
    db.save(
        run_id=cp["run_id"], ordinal=cp["ordinal"],
        char_offset=end_offset, action=action, expected_revision=cp["revision"]
    )
    _clear_pending(db)
    return _read(db)


def _skip(db: Store) -> dict:
    state = _current(db)
    cp = state["progress"]
    if not _item(state):
        return _response("Очередь закончилась.")
    db.save(
        run_id=cp["run_id"], ordinal=cp["ordinal"],
        char_offset=cp["char_offset"], action="skip", expected_revision=cp["revision"]
    )
    _clear_pending(db)
    return _read(db)


def _route(db: Store, command: str) -> dict:
    c = " ".join(command.lower().replace("ё", "е").split())
    if c in {"", "читай", "читать", "play", "плей", "продолжи", "продолжить", "начни", "слушать", "включи новости"}:
        return _read(db)
    if c in {"дальше", "следующая", "следующий", "продолжай", "далее"}:
        return _next(db)
    if c in {"пропусти", "пропустить", "пропусти новость", "пропустить новость"}:
        return _skip(db)
    if c in {"пауза", "стоп", "останови", "остановись", "остановить"}:
        # Sending a TTS message does NOT prove that the preceding chunk finished.
        # Reopen will replay the unacknowledged chunk.
        return _response("Остановлено. Когда захочешь слушать, скажи: продолжи.")
    if c in {"выход", "закрой навык", "хватит"}:
        return _response("До встречи. Место остановки сохранено.", end=True)
    if c in {"статус", "сколько осталось", "прогресс"}:
        state = _current(db)
        cp = state["progress"]
        if cp is None:
            return _response("Очередь пуста. Сначала загрузи новости.")
        return _response(f"Пройдено {cp['ordinal']} из {cp['total']} новостей. Для чтения скажи: читай.")
    return _response("Могу читать новости, продолжать, пропускать и останавливаться. Скажи: читай или дальше.")


def handle_alice(payload: dict, db: Store, *, allowed_user: str = "", allowed_application: str = "") -> dict:
    """Verify user first, then deduplicate a dialog request and change local progress."""
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object")
    identity = _identity(payload, allowed_user, allowed_application)
    key = _request_key(payload, identity)
    if not isinstance(payload.get("request"), dict):
        raise ValueError("Missing request")
    _schema(db.connection)
    cached = db.connection.execute(
        "SELECT reply_json FROM alice_replies WHERE request_key=?", (key,)
    ).fetchone()
    if cached:
        return json.loads(cached["reply_json"])
    req = payload["request"]
    if req.get("type") != "SimpleUtterance":
        reply = _response("Скажи: читай новости.")
    else:
        command = req.get("command", "")
        if not isinstance(command, str):
            raise ValueError("Invalid command")
        reply = _route(db, command)
    with db.connection:
        db.connection.execute(
            "INSERT INTO alice_replies (request_key, reply_json) VALUES (?, ?)",
            (key, json.dumps(reply, ensure_ascii=False)),
        )
    return reply
