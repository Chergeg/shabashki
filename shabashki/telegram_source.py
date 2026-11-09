"""Чтение чатов через твой Telegram-аккаунт (Telethon). Только чтение.

Что делает код с аккаунтом — и больше ничего:
  * читает список твоих папок (чтобы найти папку с шабашками);
  * читает новые сообщения в чатах этой папки;
  * при входе отправляет твоему же боту «/start», чтобы бот мог тебе писать.
Ничего не пишет в чаты, не вступает в группы, не трогает контакты и переписки.
"""
from __future__ import annotations

import asyncio
import time

from telethon import TelegramClient, errors, functions, types
from telethon.sessions import StringSession

from .notify import Post

DEVICE = dict(device_model="Shabashki Bot (read-only)", system_version="cloud", app_version="1.0",
              lang_code="ru", system_lang_code="ru")


class NeedPassword(Exception):
    pass


def make_client(session: str | None, api_id: int, api_hash: str, updates: bool = False) -> TelegramClient:
    return TelegramClient(StringSession(session or ""), api_id, api_hash, flood_sleep_threshold=60,
                          request_retries=3, connection_retries=3, receive_updates=updates, **DEVICE)


async def qr_login(client: TelegramClient, show_qr, password: str | None, total_timeout: int = 300):
    """Вход по QR: show_qr(url) показывает QR (присылаем ботом), ты сканируешь телефоном."""
    qr = await client.qr_login()
    deadline = time.time() + total_timeout
    while time.time() < deadline:
        await show_qr(qr.url)
        try:
            return await qr.wait(timeout=max(10, min(55, (qr.expires.timestamp() - time.time()))))
        except asyncio.TimeoutError:
            await qr.recreate()
        except errors.SessionPasswordNeededError:
            if not password:
                raise NeedPassword() from None
            return await client.sign_in(password=password)
    raise TimeoutError("QR так и не отсканировали")


def _title(t) -> str:
    return getattr(t, "text", None) or str(t or "")


def _username(ent) -> str | None:
    u = getattr(ent, "username", None)
    if u:
        return u
    for un in getattr(ent, "usernames", None) or []:
        if getattr(un, "active", False):
            return un.username
    return None


def peer_key(ent) -> str | None:
    if isinstance(ent, types.Channel):
        return f"c{ent.id}"
    if isinstance(ent, types.Chat):
        return f"g{ent.id}"
    return None


async def list_folders(client: TelegramClient) -> list:
    res = await client(functions.messages.GetDialogFiltersRequest())
    filters = getattr(res, "filters", res) or []
    return [f for f in filters if isinstance(f, (types.DialogFilter, types.DialogFilterChatlist))]


def folder_titles(folders) -> list[str]:
    return [_title(f.title) for f in folders]


def _match(title: str, wanted: str) -> bool:
    clean = lambda s: "".join(ch for ch in s.lower().replace("ё", "е") if ch.isalnum())
    return clean(title) == clean(wanted) or (len(clean(wanted)) >= 3 and clean(wanted) in clean(title))


async def folder_chats(client: TelegramClient, folder_name: str) -> list:
    """Сущности чатов/каналов из папки. Личные переписки и боты пропускаются."""
    folders = await list_folders(client)
    target = next((f for f in folders if _match(_title(f.title), folder_name)), None)
    if target is None:
        return []
    ents, seen = [], set()
    for p in list(getattr(target, "pinned_peers", []) or []) + list(getattr(target, "include_peers", []) or []):
        if isinstance(p, (types.InputPeerUser, types.InputPeerSelf)):
            continue
        try:
            ent = await client.get_entity(p)
        except (ValueError, errors.RPCError):
            continue
        k = peer_key(ent)
        if k and k not in seen:
            seen.add(k)
            ents.append(ent)
    # Папка вида «все группы/каналы» (флаги) — добираем из списка диалогов
    if isinstance(target, types.DialogFilter) and (target.groups or target.broadcasts):
        excl = {getattr(p, "channel_id", None) or getattr(p, "chat_id", None) for p in target.exclude_peers or []}
        async for d in client.iter_dialogs(limit=400):
            ent = d.entity
            k = peer_key(ent)
            if not k or k in seen or ent.id in excl:
                continue
            is_broadcast = isinstance(ent, types.Channel) and ent.broadcast
            if (is_broadcast and target.broadcasts) or (not is_broadcast and target.groups):
                seen.add(k)
                ents.append(ent)
    return ents[:40]


def chat_info(ent) -> dict:
    return {"title": getattr(ent, "title", "") or "чат", "username": _username(ent),
            "id": ent.id, "kind": "channel" if getattr(ent, "broadcast", False) else "group"}


async def fetch_new(client: TelegramClient, ent, last_id: int | None, since_ts: int, limit: int = 150):
    """Новые сообщения (старые → новые). Возвращает (posts, max_id)."""
    info = chat_info(ent)
    kwargs = {"limit": limit}
    if last_id:
        kwargs["min_id"] = last_id
    raw = []
    async for m in client.iter_messages(ent, **kwargs):
        if not last_id and m.date.timestamp() < since_ts:
            break
        raw.append(m)
    raw.reverse()
    max_id = max([m.id for m in raw] + [last_id or 0])
    posts = []
    for m in raw:
        if getattr(m, "action", None) or m.out:
            continue
        text = (m.message or "").strip()
        if len(text) < 12:
            continue
        snd = m.sender
        name = ""
        if snd is not None:
            name = " ".join(x for x in [getattr(snd, "first_name", None), getattr(snd, "last_name", None)] if x) \
                   or getattr(snd, "title", "") or ""
        sender_un = _username(snd) if isinstance(snd, types.User) and not getattr(snd, "bot", False) else None
        posts.append(Post(chat_key=peer_key(ent) or "?", chat_title=info["title"], chat_username=info["username"],
                          chat_id=ent.id, msg_id=m.id, date=int(m.date.timestamp()), text=text,
                          sender_name=name, sender_username=sender_un))
    return posts, max_id
