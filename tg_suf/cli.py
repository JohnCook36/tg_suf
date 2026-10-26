"""Stage 0 commands: demo, Telegram login, channel selection, sync and playback."""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from .config import data_dir
from .models import NewsPost
from .storage import ActiveQueueError, Store


def db_path():
    return data_dir() / "reader.sqlite3"


async def _login():
    from .telegram_source import connect
    client = await connect()
    try:
        who = await client.get_me()
        print(f"Telegram подключён: {who.first_name or 'пользователь'} (ID {who.id}).")
        print("Секретный файл Telegram-сессии хранится только локально в .local/.")
    finally:
        await client.disconnect()


async def _channels():
    from .telegram_source import broadcast_dialogs, connect
    client = await connect()
    try:
        dialogs = await broadcast_dialogs(client)
        for i, dialog in enumerate(dialogs, 1):
            print(f"{i:>3}. {dialog.name} [ID {dialog.entity.id}]")
        if not dialogs:
            print("Нет доступных вещательных каналов.")
    finally:
        await client.disconnect()


async def _sync(limit: int, ids: str | None, replace: bool):
    from .telegram_source import broadcast_dialogs, connect, fetch_selected
    client = await connect()
    try:
        dialogs = await broadcast_dialogs(client)
        if not dialogs:
            print("Доступные каналы не найдены.")
            return
        if ids is None:
            print("Выбери каналы в нужном порядке:")
            for i, dialog in enumerate(dialogs, 1):
                print(f"{i}. {dialog.name} [ID {dialog.entity.id}]")
            raw = input("Номера через запятую (например 2,1): ")
            try:
                indices = [int(part.strip()) for part in raw.split(",")]
                if not indices or any(i < 1 or i > len(dialogs) for i in indices):
                    raise ValueError
            except ValueError as exc:
                raise ValueError("Неправильные номера каналов") from exc
            selected = [int(dialogs[i-1].entity.id) for i in indices]
        else:
            selected = [int(part.strip()) for part in ids.split(",") if part.strip()]
        if not selected or len(selected) != len(set(selected)):
            raise ValueError("Выбери уникальные каналы")
        with Store(db_path()) as store:
            cp = store.current()
            if cp and cp["ordinal"] < cp["total"] and not replace:
                raise ActiveQueueError("Сначала закончи очередь или укажи --replace-queue")
        groups = await fetch_selected(client, selected, limit)
        with Store(db_path()) as store:
            result = store.create_queue(groups, replace=replace)
        print(f"Очередь #{result['run_id']}: {result['count']} новостей.")
        print("Запусти: python -m tg_suf serve")
    finally:
        await client.disconnect()


def _demo(replace: bool):
    now = datetime.now(timezone.utc).isoformat()
    groups = [
        (1001, "Наука сегодня", [
            NewsPost(1001, 42, "Наука сегодня", "Учёные представили новые исследования в области аккумуляторов.", now),
            NewsPost(1001, 43, "Наука сегодня", "Это демонстрационная новость. Во время чтения нажми Паузу, затем снова Play.", now),
        ]),
        (2002, "Технологии", [
            NewsPost(2002, 9, "Технологии", "Сейчас прозвучала новость из второго канала.", now),
        ]),
    ]
    with Store(db_path()) as store:
        result = store.create_queue(groups, replace=replace)
    print(f"Демонстрационная очередь #{result['run_id']}: {result['count']} новости.")
    print("Запусти: python -m tg_suf serve")


def _status():
    with Store(db_path()) as store:
        cp = store.current()
    if cp:
        print(f"Очередь {cp['run_id']}: {min(cp['ordinal']+1,cp['total'])}/{cp['total']}, "
              f"символ {cp['char_offset']}, {cp['state']}, rev={cp['revision']}.")
    else:
        print("Очереди пока нет. Выполни python -m tg_suf demo")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ручной диктор — новости Telegram голосом")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login")
    sub.add_parser("channels")
    sync = sub.add_parser("sync")
    sync.add_argument("--ids", help="Telegram channel IDs в нужном порядке через запятую")
    sync.add_argument("--limit", type=int, default=20)
    sync.add_argument("--replace-queue", action="store_true")
    demo = sub.add_parser("demo")
    demo.add_argument("--replace-queue", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--no-browser", action="store_true")
    sub.add_parser("status")
    sub.add_parser("alice-serve", help="Локальный backend приватного навыка Алисы")
    args = parser.parse_args(argv)
    try:
        if args.command == "login":
            asyncio.run(_login())
        elif args.command == "channels":
            asyncio.run(_channels())
        elif args.command == "sync":
            if not 1 <= args.limit <= 200:
                parser.error("--limit должен быть от 1 до 200")
            asyncio.run(_sync(args.limit, args.ids, args.replace_queue))
        elif args.command == "demo":
            _demo(args.replace_queue)
        elif args.command == "serve":
            from .server import serve as start
            start(open_browser=not args.no_browser)
        elif args.command == "status":
            _status()
        elif args.command == "alice-serve":
            from .alice_server import serve_alice
            serve_alice()

        return 0
    except (ValueError, RuntimeError, ActiveQueueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
