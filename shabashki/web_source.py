"""Чтение ПУБЛИЧНЫХ каналов через открытую веб-версию t.me/s/<канал> — без входа в Telegram.

Твой аккаунт здесь вообще не участвует: бот смотрит канал так же, как любой человек
в браузере без регистрации. Поэтому прочитать твою переписку этим кодом невозможно.
Закрытые каналы так не читаются — для них есть отдельный режим (README, «закрытые каналы»).
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser

from .notify import Post

UA = "Mozilla/5.0 (compatible; ShabashkiBot/1.0; +https://github.com)"
_VOID = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "param", "track"}
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


class ChannelUnavailable(Exception):
    """Канал закрытый, это группа, его нет или Telegram не показывает его в веб-версии."""


def clean_name(raw: str) -> str | None:
    """«@name», «t.me/name», «https://t.me/s/name/123» -> «name». Ссылки-приглашения (+xxx) — нет."""
    s = (raw or "").strip()
    m = re.search(r"(?:t(?:elegram)?\.me/(?:s/)?)([A-Za-z][A-Za-z0-9_]{3,31})(?:[/?#].*)?$", s)
    if m:
        s = m.group(1)
    s = s.lstrip("@").strip()
    if s.lower() in {"joinchat", "addlist", "share", "s", "c"}:
        return None
    return s if NAME_RE.match(s) else None


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set]] = []
        self.msgs: list[dict] = []
        self.title: str | None = None
        self.cur: dict | None = None
        self.cur_depth: int | None = None
        self.text_depth: int | None = None
        self.buf: list[str] = []

    def _in_class(self, name: str) -> bool:
        return any(name in cls for _, cls in self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = set((a.get("class") or "").split())
        if tag == "meta" and a.get("property") == "og:title" and not self.title:
            self.title = (a.get("content") or "").strip() or None
        if tag in _VOID:
            if tag == "br" and self.text_depth is not None:
                self.buf.append("\n")
            return
        parent = self.stack[-1][1] if self.stack else set()
        self.stack.append((tag, cls))
        depth = len(self.stack)
        if self.cur is None:
            if tag == "div" and "tgme_widget_message" in cls and a.get("data-post"):
                self.cur = {"post": a["data-post"], "text": None, "date": None, "service": "service_message" in cls}
                self.cur_depth = depth
            return
        if "service_message" in cls:
            self.cur["service"] = True
        if (tag == "div" and "tgme_widget_message_text" in cls and self.text_depth is None and self.cur["text"] is None
                and ("js-message_text" in cls or parent & {"tgme_widget_message_bubble", "media_supported_cont"})
                and "js-message_reply_text" not in cls and not self._in_class("tgme_widget_message_reply")):
            self.text_depth = depth
            self.buf = []
        if tag == "time" and a.get("datetime") and self.cur["date"] is None and self._in_class("tgme_widget_message_date"):
            self.cur["date"] = a["datetime"]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID or not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            t, _ = self.stack.pop()
            depth = len(self.stack) + 1
            if self.text_depth is not None and depth == self.text_depth and self.cur is not None:
                self.cur["text"] = "".join(self.buf).strip()
                self.text_depth = None
            if self.cur is not None and depth == self.cur_depth:
                self.msgs.append(self.cur)
                self.cur, self.cur_depth = None, None
            if t == tag:
                break

    def handle_data(self, data):
        if self.text_depth is not None:
            self.buf.append(data)


def parse_page(html: str) -> tuple[str | None, list[dict]]:
    """HTML страницы t.me/s/... -> (название канала, [{id, date, text}]). Служебные сообщения выкидываем."""
    p = _Page()
    p.feed(html)
    p.close()
    out = []
    for m in p.msgs:
        if m.get("service") or not m.get("text"):
            continue
        try:
            mid = int(str(m["post"]).rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        ts = None
        if m.get("date"):
            try:
                ts = int(datetime.fromisoformat(m["date"]).timestamp())
            except ValueError:
                ts = None
        out.append({"id": mid, "date": ts or int(time.time()), "text": re.sub(r"\n{3,}", "\n\n", m["text"])})
    out.sort(key=lambda x: x["id"])
    return p.title, out


def _get(url: str, timeout: int = 20) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace"), r.geturl()


def fetch_page(name: str, before: int | None = None) -> tuple[str | None, list[dict]]:
    url = f"https://t.me/s/{name}" + (f"?before={before}" if before else "")
    try:
        html, final = _get(url)
    except urllib.error.HTTPError as e:
        if e.code in (302, 404):
            raise ChannelUnavailable(name) from None
        raise
    if "/s/" not in final:
        raise ChannelUnavailable(name)   # Telegram отправил на обычную страницу — веб-просмотра нет
    title, posts = parse_page(html)
    if not posts and "tgme_channel_info" not in html and "tgme_widget_message" not in html:
        raise ChannelUnavailable(name)
    return title, posts


def fetch_new(name: str, last_id: int | None, since_ts: int, max_pages: int = 3):
    """Новые посты канала (старые -> новые): (posts, max_id, title)."""
    title, page = fetch_page(name)
    if not page and not last_id:
        raise ChannelUnavailable(name)   # группа или пустой канал: веб-версия не показывает ни одного поста
    got = list(page)
    pages = 1
    # если вся страница новая — листаем назад, пока не дойдём до известного или до границы по времени
    while got and pages < max_pages:
        oldest = got[0]
        if (last_id and oldest["id"] <= last_id + 1) or (not last_id and oldest["date"] < since_ts):
            break
        _, older = fetch_page(name, before=oldest["id"])
        older = [p for p in older if p["id"] < oldest["id"]]
        if not older:
            break
        got = older + got
        pages += 1
        time.sleep(0.5)
    if last_id:
        fresh = [p for p in got if p["id"] > last_id]
    else:
        fresh = [p for p in got if p["date"] >= since_ts]
    max_id = max([p["id"] for p in got] + [last_id or 0])
    t = title or name
    posts = [Post(chat_key=f"w:{name.lower()}", chat_title=t, chat_username=name, chat_id=0, msg_id=p["id"],
                  date=p["date"], text=p["text"]) for p in fresh if len(p["text"]) >= 12]
    return posts, max_id, t
