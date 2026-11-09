"""Состояние бота (сессия Telegram, последние id сообщений, настройки) — только в зашифрованном виде.

Шифрование: PBKDF2-HMAC-SHA256 (200 000 итераций) -> AES-256-CTR (pyaes, уже идёт
с Telethon) + HMAC-SHA256 поверх всего (encrypt-then-MAC). Без ключа файл —
просто мусор, а любая подмена обнаруживается и отклоняется.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import zlib

import pyaes

MAGIC = b"SHB1"
_ITER = 200_000


class StateError(Exception):
    pass


def _keys(secret: str, salt: bytes) -> tuple[bytes, bytes]:
    k = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, _ITER, dklen=64)
    return k[:32], k[32:]


def encrypt(obj: dict, secret: str) -> bytes:
    raw = zlib.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)
    salt, nonce = os.urandom(16), os.urandom(16)
    ek, mk = _keys(secret, salt)
    ctr = pyaes.Counter(initial_value=int.from_bytes(nonce, "big"))
    ct = pyaes.AESModeOfOperationCTR(ek, counter=ctr).encrypt(raw)
    head = MAGIC + salt + nonce
    tag = hmac.new(mk, head + ct, hashlib.sha256).digest()
    return head + ct + tag


def decrypt(blob: bytes, secret: str) -> dict:
    if len(blob) < 4 + 16 + 16 + 32 or blob[:4] != MAGIC:
        raise StateError("файл состояния повреждён или не наш")
    salt, nonce, ct, tag = blob[4:20], blob[20:36], blob[36:-32], blob[-32:]
    ek, mk = _keys(secret, salt)
    if not hmac.compare_digest(tag, hmac.new(mk, blob[:36] + ct, hashlib.sha256).digest()):
        raise StateError("не тот ключ или файл подменили")
    ctr = pyaes.Counter(initial_value=int.from_bytes(nonce, "big"))
    raw = pyaes.AESModeOfOperationCTR(ek, counter=ctr).decrypt(ct)
    return json.loads(zlib.decompress(raw).decode("utf-8"))


def empty_state() -> dict:
    return {
        "v": 1,
        "session": None,        # строка сессии Telethon — равна доступу к аккаунту, поэтому только тут
        "owner_id": None,       # твой user id: бот слушается только его
        "channels": {},         # публичные каналы: имя -> {"title", "last_id", "bad", "pending"}
        "folder": None,         # (только режим закрытых чатов) папка Telegram
        "chats": {},            # (только режим закрытых чатов) peer_key -> {"last_id", "title", "username"}
        "seen": [],             # отпечатки постов для антиповторов
        "geo": {},              # кэш геокодинга
        "home": None,           # [lat, lon] — если отправишь боту геопозицию
        "settings": {"near_rate": None, "paused": False},
        "bot_offset": 0,
        "stats": {"day": "", "checked": 0, "sent": 0, "llm": 0, "dropped": 0},
        "notices": {},          # анти-спам служебных уведомлений: key -> ts
        "last_run": 0,
        "created": int(time.time()),
    }


def load(path: str, secrets: list[str]) -> dict:
    """Пробуем ключи по очереди (основной первым). Сохраняется всегда основным — так ключ можно сменить."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return empty_state()
    with open(path, "rb") as fh:
        blob = fh.read()
    st, err = None, None
    for secret in secrets:
        try:
            st = decrypt(blob, secret)
            break
        except StateError as e:
            err = e
    if st is None:
        raise err or StateError("нет ключа")
    base = empty_state()
    base.update(st)
    for k in ("settings", "stats"):
        merged = empty_state()[k]
        merged.update(st.get(k) or {})
        base[k] = merged
    return base


def save(path: str, secret: str, st: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(encrypt(st, secret))
    os.replace(tmp, path)
