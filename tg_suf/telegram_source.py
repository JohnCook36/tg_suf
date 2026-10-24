"""Telegram MTProto user client (not a bot). Only broadcast channels."""
from datetime import datetime, timezone

from .config import api_credentials, data_dir
from .models import NewsPost


def _telethon():
    try:
        from telethon import TelegramClient
        from telethon.tl.types import Channel
    except ImportError as exc:
        raise RuntimeError('Установи: python -m pip install -e ".[telegram]"') from exc
    return TelegramClient, Channel


async def connect():
    TelegramClient, _ = _telethon()
    api_id, api_hash = api_credentials()
    client = TelegramClient(str(data_dir() / "telegram_user"), api_id, api_hash)
    try:
        await client.start()
        return client
    except Exception:
        await client.disconnect()
        raise


async def broadcast_dialogs(client):
    _, Channel = _telethon()
    dialogs = await client.get_dialogs()
    return [d for d in dialogs if isinstance(d.entity, Channel) and bool(d.entity.broadcast)]


async def fetch_selected(client, selected_ids: list[int], limit: int):
    dialogs = await broadcast_dialogs(client)
    by_id = {int(d.entity.id): d for d in dialogs}
    result = []
    for peer_id in selected_ids:
        if peer_id not in by_id:
            raise ValueError(f"Канал {peer_id} недоступен.")
        dialog = by_id[peer_id]
        posts = []
        async for message in client.iter_messages(dialog.entity, limit=limit):
            if not message.message or not message.message.strip():
                continue
            date = message.date or datetime.now(timezone.utc)
            posts.append(NewsPost(
                peer_id=peer_id, message_id=int(message.id),
                channel_title=dialog.name, text=message.message.strip(),
                published_at=date.astimezone(timezone.utc).isoformat(),
            ))
        result.append((peer_id, dialog.name, posts))
    return result
