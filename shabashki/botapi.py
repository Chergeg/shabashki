"""Минимальный клиент Telegram Bot API на стандартной библиотеке (без requests и чужих SDK)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid


class BotError(Exception):
    pass


class Bot:
    def __init__(self, token: str, timeout: int = 30):
        if not token or ":" not in token:
            raise BotError("BOT_TOKEN не задан или кривой")
        self._base = f"https://api.telegram.org/bot{token}/"
        self.timeout = timeout

    # -- низкий уровень --------------------------------------------------
    def call(self, method: str, params: dict | None = None, files: dict | None = None, timeout: int | None = None):
        url = self._base + method
        for attempt in range(4):
            try:
                if files:
                    body, ctype = _multipart(params or {}, files)
                else:
                    body, ctype = json.dumps(params or {}, ensure_ascii=False).encode("utf-8"), "application/json"
                req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    data = json.loads(e.read().decode("utf-8"))
                except Exception:
                    data = {"ok": False, "description": f"HTTP {e.code}"}
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 3:
                    raise BotError(f"сеть: {type(e).__name__}") from None
                time.sleep(2 + attempt * 2)
                continue
            if data.get("ok"):
                return data.get("result")
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry and attempt < 3:
                time.sleep(min(int(retry) + 1, 30))
                continue
            # описание ошибки без токена (токена в нём и так нет)
            raise BotError(str(data.get("description", "ошибка Bot API"))[:200])
        raise BotError("не удалось достучаться до Bot API")

    # -- удобные обёртки -------------------------------------------------
    def me(self) -> dict:
        return self.call("getMe")

    def send(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None, preview: bool = False) -> dict:
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": not preview}}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        try:
            return self.call("sendMessage", params)
        except BotError as e:
            if "can't parse entities" in str(e).lower():
                # на всякий случай: если HTML поломался — шлём без разметки
                params.pop("parse_mode", None)
                params["text"] = _strip_tags(text)
                return self.call("sendMessage", params)
            raise

    def send_photo(self, chat_id: int, png: bytes, caption: str = "") -> dict:
        return self.call("sendPhoto", {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"},
                         files={"photo": ("qr.png", png, "image/png")})

    def delete(self, chat_id: int, message_id: int) -> None:
        try:
            self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except BotError:
            pass

    def updates(self, offset: int = 0, timeout: int = 0) -> list[dict]:
        return self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                        "allowed_updates": ["message"]}, timeout=timeout + 15) or []

    def set_commands(self) -> None:
        cmds = [
            {"command": "status", "description": "что с ботом и сколько нашёл"},
            {"command": "kanaly", "description": "какие каналы читаю"},
            {"command": "stavka", "description": "минимум ₽/ч для «рядом», например /stavka 600"},
            {"command": "pause", "description": "поставить на паузу"},
            {"command": "resume", "description": "снова присылать"},
            {"command": "dom", "description": "как задать точку дома"},
            {"command": "help", "description": "что и как бот присылает"},
        ]
        try:
            self.call("setMyCommands", {"commands": cmds})
        except BotError:
            pass


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----shb" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode()
        out += (v if isinstance(v, str) else json.dumps(v)).encode("utf-8") + b"\r\n"
    for k, (fname, content, ctype) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{os.path.basename(fname)}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _strip_tags(s: str) -> str:
    import html
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", s))
