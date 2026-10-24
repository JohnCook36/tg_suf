"""Immutable SQLite queue snapshots and optimistic playback checkpoints."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import NewsPost


class ConflictError(Exception):
    """An outdated or mismatched playback update."""


class ActiveQueueError(Exception):
    """A new queue would discard an unfinished playback session."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, filename: str | Path):
        self.connection = sqlite3.connect(str(filename), timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=15000")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
                peer_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                published_at TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                PRIMARY KEY (peer_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS queue_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS queue_items (
                run_id INTEGER NOT NULL REFERENCES queue_runs(id),
                ordinal INTEGER NOT NULL,
                peer_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                frozen_title TEXT NOT NULL,
                frozen_text TEXT NOT NULL,
                published_at TEXT NOT NULL,
                PRIMARY KEY (run_id, ordinal),
                UNIQUE (run_id, peer_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS checkpoint (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                run_id INTEGER NOT NULL REFERENCES queue_runs(id),
                ordinal INTEGER NOT NULL,
                char_offset INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'paused',
                revision INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
        """)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _checkpoint(self):
        return self.connection.execute("SELECT * FROM checkpoint WHERE singleton=1").fetchone()

    def current(self) -> dict | None:
        cp = self._checkpoint()
        if cp is None:
            return None
        result = dict(cp)
        result["total"] = self.connection.execute(
            "SELECT COUNT(*) FROM queue_items WHERE run_id=?", (cp["run_id"],)
        ).fetchone()[0]
        return result

    def items(self, run_id: int) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM queue_items WHERE run_id=? ORDER BY ordinal", (run_id,)
        )]

    def state(self) -> dict:
        cp = self.current()
        return {"progress": cp, "items": self.items(cp["run_id"]) if cp else []}

    def create_queue(self, groups: Iterable[tuple[int, str, Iterable[NewsPost]]], *, replace: bool = False) -> dict:
        """Create a channel-first snapshot, with each channel oldest-first."""
        snapshot = []
        seen = set()
        for peer_id, title, posts in groups:
            for post in sorted(posts, key=lambda p: (p.published_at, p.message_id)):
                if post.peer_id != peer_id:
                    raise ValueError("Post belongs to a different channel")
                key = (post.peer_id, post.message_id)
                if key in seen or not post.text.strip():
                    continue
                seen.add(key)
                snapshot.append(NewsPost(peer_id, post.message_id, title, post.text.strip(), post.published_at))
        if not snapshot:
            raise ValueError("Нет текстовых публикаций для воспроизведения.")
        with self.connection:
            prior = self._checkpoint()
            if prior and not replace:
                total = self.connection.execute(
                    "SELECT COUNT(*) FROM queue_items WHERE run_id=?", (prior["run_id"],)
                ).fetchone()[0]
                if prior["ordinal"] < total:
                    raise ActiveQueueError("Незавершённая очередь. Для замены передай --replace-queue.")
            run_id = self.connection.execute(
                "INSERT INTO queue_runs (created_at) VALUES (?)", (utc_now(),)
            ).lastrowid
            for i, post in enumerate(snapshot):
                hashed = hashlib.sha256(post.text.encode("utf-8")).hexdigest()
                self.connection.execute("""
                    INSERT INTO posts (peer_id, message_id, title, text, published_at, text_hash)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(peer_id, message_id) DO UPDATE SET
                        title=excluded.title, text=excluded.text,
                        published_at=excluded.published_at, text_hash=excluded.text_hash
                """, (post.peer_id, post.message_id, post.channel_title, post.text, post.published_at, hashed))
                self.connection.execute("""
                    INSERT INTO queue_items
                    (run_id, ordinal, peer_id, message_id, frozen_title, frozen_text, published_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (run_id, i, post.peer_id, post.message_id, post.channel_title, post.text, post.published_at))
            self.connection.execute("""
                INSERT INTO checkpoint (singleton, run_id, ordinal, char_offset, state, revision, updated_at)
                VALUES (1, ?, 0, 0, 'paused', 1, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    run_id=excluded.run_id, ordinal=0, char_offset=0,
                    state='paused', revision=checkpoint.revision+1, updated_at=excluded.updated_at
            """, (run_id, utc_now()))
        return {"run_id": run_id, "count": len(snapshot)}

    def save(self, *, run_id: int, ordinal: int, char_offset: int, action: str, expected_revision: int) -> dict:
        """CAS save. Complete/skip advance; playing/pause retain the current post."""
        if action not in {"playing", "pause", "complete", "skip"}:
            raise ValueError("Unknown action")
        if type(char_offset) is not int or char_offset < 0:
            raise ValueError("Invalid offset")
        with self.connection:
            cp = self._checkpoint()
            if cp is None or (cp["run_id"], cp["ordinal"], cp["revision"]) != (
                run_id, ordinal, expected_revision
            ):
                raise ConflictError("Checkpoint changed; reload state")
            item = self.connection.execute(
                "SELECT frozen_text FROM queue_items WHERE run_id=? AND ordinal=?",
                (run_id, ordinal)
            ).fetchone()
            if item is None:
                raise ConflictError("Queue already finished")
            if char_offset > len(item["frozen_text"]):
                raise ValueError("Offset beyond end of post")
            if action in {"complete", "skip"}:
                next_ordinal, next_offset, state = ordinal + 1, 0, "paused"
            else:
                next_ordinal, next_offset = ordinal, char_offset
                state = "playing" if action == "playing" else "paused"
            update = self.connection.execute("""
                UPDATE checkpoint SET ordinal=?, char_offset=?, state=?,
                    revision=revision+1, updated_at=?
                WHERE singleton=1 AND revision=?
            """, (next_ordinal, next_offset, state, utc_now(), expected_revision))
            if update.rowcount != 1:
                raise ConflictError("Checkpoint conflict")
        return self.state()
