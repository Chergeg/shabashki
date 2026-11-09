"""Карточки шабашек для бота (HTML-разметка Telegram)."""
from __future__ import annotations

import html
import time
from dataclasses import dataclass

from .extract import Features, phones, usernames
from .geo import Loc, OUT
from .scoring import ROUTE_TITLES, Verdict


@dataclass
class Post:
    chat_key: str
    chat_title: str
    chat_username: str | None
    chat_id: int
    msg_id: int
    date: int
    text: str
    sender_name: str = ""
    sender_username: str | None = None
    backlog: bool = False      # пост за последние сутки из только что добавленного канала

    @property
    def link(self) -> str | None:
        if self.chat_username:
            return f"https://t.me/{self.chat_username}/{self.msg_id}"
        if self.chat_key.startswith("c") and self.chat_id > 0:   # супергруппы и каналы
            return f"https://t.me/c/{self.chat_id}/{self.msg_id}"
        return None  # у старых обычных групп ссылок на сообщения нет


def _esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def _ago(ts: int, now: float | None = None) -> str:
    d = int((now or time.time()) - ts)
    if d < 90:
        return "только что"
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 86400:
        return f"{d // 3600} ч назад"
    return f"{d // 86400} дн назад"


def _loc_line(loc: Loc) -> str:
    if loc.minutes is None:
        return "📍 место не указано"
    if loc.minutes >= OUT:
        return f"📍 {_esc(loc.label)}"
    how = "по адресу" if loc.precise else "примерно"
    walk = f"🚶 ~{loc.minutes} мин ({how})" if loc.minutes <= 45 else f"🚌 далеко, ~{loc.minutes} мин пешком"
    return f"📍 {_esc(loc.label)} · {walk}"


def card(post: Post, f: Features, loc: Loc, v: Verdict, now: float | None = None) -> tuple[str, list[list[dict]]]:
    head = "🔥" if v.hot else "👍"
    route = ROUTE_TITLES.get(v.route or "", "")
    job = v.job_label[:60] if v.job_label else "шабашка"
    lines = [f"{head} {route} · <b>{_esc(job[:1].upper() + job[1:])}</b> · {v.score}/100",
             f"💰 <b>{_esc(v.pay_text)}</b>",
             _loc_line(loc)]
    if f.when:
        lines.append(f"🕒 {_esc(f.when)}")
    if f.people:
        lines.append(f"👥 нужно человек: {f.people}")
    tags = [t for t in v.tags if t not in ("✨ интересно", "📍 рядом")] if route else v.tags
    if tags:
        lines.append(" · ".join(_esc(t) for t in tags[:5]))
    if v.why:
        lines.append(f"💬 <i>{_esc(v.why[:220])}</i>")
    if v.warnings:
        lines.append("⚠️ " + _esc(", ".join(dict.fromkeys(v.warnings))))
    tel = phones(post.text)
    if tel:
        lines.append("📞 " + ", ".join(tel[:2]))
    lines.append(f"<i>{_esc(post.chat_title[:40])} · {_ago(post.date, now)}</i>")
    body = post.text.strip()
    if len(body) > 1500:
        body = body[:1500].rsplit(" ", 1)[0] + " …"
    text = "\n".join(lines) + f"\n\n<blockquote expandable>{_esc(body)}</blockquote>"

    buttons: list[dict] = []
    if post.link:
        buttons.append({"text": "📨 Открыть пост", "url": post.link})
    contact = post.sender_username or (usernames(post.text) or [None])[0]
    if contact:
        buttons.append({"text": f"✉️ Написать @{contact}"[:40], "url": f"https://t.me/{contact}"})
    return text, ([buttons] if buttons else [])


HELP = (
    "<b>Бот пиздатых шабашек</b> 🛠\n\n"
    "Читаю каналы с шабашками и присылаю только:\n"
    "🏨 <b>ДОРОГИЕ МЕСТА</b> — отели (BURNY, Lotte, Azimut…), ДВФУ, ВЭФ, топ-рестораны, яхт-клубы;\n"
    "🚚 <b>ПОЕЗДКИ</b> — погрузил, едешь в другой город, разгрузил, обратно;\n"
    "✨ <b>ИНТЕРЕСНОЕ</b> — съёмки, массовка, большие ивенты, рилсы;\n"
    "📍 <b>РЯДОМ</b> — до 30 минут пешком, от 500 ₽/ч. Без фур, стройки, смен по 8+ часов и без частников «на час-два».\n\n"
    "Добавить канал — перешли мне пост из него или пришли ссылку.\n"
    "/kanaly — какие каналы читаю · /ubrat имя — убрать канал\n"
    "/stavka 600 — минимум ₽/ч для «рядом» (по умолчанию 500)\n"
    "/status · /pause · /resume · /dom — точка дома\n\n"
    "<i>Команды выполняются при следующей проверке (раз в ~10 минут).</i>"
)

DOM = ("📍 Чтобы я точно считал, сколько идти пешком, пришли мне <b>геопозицию дома</b>: "
       "скрепка → Геопозиция → «Отправить выбранную геопозицию» (поставь точку на дом). "
       "Координаты хранятся только в зашифрованном виде.")


def status_text(st: dict, near_rate: int, now: float | None = None) -> str:
    s = st.get("stats", {})
    last = st.get("last_run") or 0
    paused = st.get("settings", {}).get("paused")
    chans = st.get("channels", {})
    ok = [c.get("title") or k for k, c in chans.items() if not c.get("bad")]
    bad = [k for k, c in chans.items() if c.get("bad")]
    lines = ["<b>Статус</b>",
             f"{'⏸ на паузе' if paused else '▶️ работаю'} · «рядом» от <b>{near_rate} ₽/ч</b>",
             f"Каналов читаю: <b>{len(ok)}</b>" + (f" (не читаются: {len(bad)})" if bad else ""),
             f"Последняя проверка: {_ago(last, now) if last else 'ещё не было'}",
             f"Сегодня: проверено {s.get('checked', 0)}, прислал {s.get('sent', 0)}, отсеял {s.get('dropped', 0)}"]
    if ok:
        lines.append("Каналы: " + _esc(", ".join(ok[:12])) + (" …" if len(ok) > 12 else ""))
    if st.get("session"):
        lines.append(f"Закрытые чаты: папка «{_esc(st.get('folder') or '?')}» через отдельный аккаунт")
    lines.append("Дом: " + ("задан геопозицией ✅" if st.get("home") else "не задан — /dom"))
    return "\n".join(lines)
