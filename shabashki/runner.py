"""Главный цикл: команды бота → новые посты → анализ → карточки в бот.

В логи (а в публичном репозитории их видят все) пишем только счётчики —
никаких текстов, названий чатов и личных данных.
"""
from __future__ import annotations

import asyncio
import io
import re
import time
from datetime import datetime, timedelta, timezone

from . import geo, profile
from . import state as state_mod
from .botapi import Bot, BotError
from .config import Config
from .dedupe import Seen
from .extract import analyze, norm
from .geo import Geocoder, Loc, locate, zone_lookup
from .llm import LLM
from .notify import DOM, HELP, Post, card, status_text
from . import web_source
from .scoring import DROP_KINDS, evaluate

VLAD_TZ = timezone(timedelta(hours=10))
LLM_MIN_PRESCORE = 30      # в нейросеть идут только посты, у которых правила дали хоть какой-то шанс
MAX_AGE_HOURS = 12         # старьё (например, после паузы) не присылаем


def log(msg: str) -> None:
    print(f"[{datetime.now(VLAD_TZ):%H:%M:%S}] {msg}", flush=True)


def _today() -> str:
    return datetime.now(VLAD_TZ).strftime("%Y-%m-%d")


def _roll_day(st: dict) -> None:
    if st["stats"].get("day") != _today():
        st["stats"] = {"day": _today(), "checked": 0, "sent": 0, "llm": 0, "dropped": 0}


def near_rate_of(st: dict) -> int:
    t = st.get("settings", {}).get("near_rate")
    return int(t) if isinstance(t, int) and 200 <= t <= 5000 else profile.NEAR_RATE


def notify_once(st: dict, bot: Bot, key: str, text: str, every_h: float = 6) -> None:
    """Служебные уведомления не чаще раза в N часов."""
    if not st.get("owner_id"):
        return
    last = st["notices"].get(key, 0)
    if time.time() - last < every_h * 3600:
        return
    try:
        bot.send(st["owner_id"], text)
        st["notices"][key] = int(time.time())
    except BotError:
        pass


# ---------------------------------------------------------------------------
# Команды из бота
# ---------------------------------------------------------------------------

def handle_commands(st: dict, bot: Bot, cfg_owner: int | None = None) -> None:
    try:
        ups = bot.updates(offset=st.get("bot_offset", 0), timeout=0)
    except BotError as e:
        log(f"бот: не получил команды ({str(e)[:60]})")
        return
    if cfg_owner:
        st["owner_id"] = cfg_owner
    owner = st.get("owner_id")
    just_registered = False
    if not owner:
        starters = []
        for u in ups:
            m = u.get("message") or {}
            if (m.get("chat") or {}).get("type") == "private" and (m.get("text") or "").startswith("/start"):
                uid = (m.get("from") or {}).get("id")
                if uid and uid not in starters:
                    starters.append(uid)
        if len(starters) == 1:
            owner = st["owner_id"] = starters[0]
            just_registered = True
            try:
                bot.set_commands()
                bot.send(owner, "👋 Ты владелец бота — теперь я слушаюсь только тебя.\n\n" + HELP)
            except BotError:
                pass
        elif len(starters) > 1:
            log("Боту написали /start разные аккаунты — добавь секрет OWNER_ID и нажми /start ещё раз")
    for u in ups:
        st["bot_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if not owner or (m.get("from") or {}).get("id") != owner or (m.get("chat") or {}).get("type") != "private":
            continue  # чужих не слушаем вообще
        if just_registered and (m.get("text") or "").startswith("/start"):
            continue  # приветствие уже отправили
        try:
            _command(st, bot, m)
        except BotError:
            pass


def add_channel(st: dict, raw: str, title: str | None = None) -> str | None:
    name = web_source.clean_name(raw)
    if not name:
        return None
    chans = st.setdefault("channels", {})
    key = name.lower()
    if key in chans and not chans[key].get("bad"):
        return name
    if len(chans) >= 50:
        return None
    chans[key] = {"name": name, "title": title or name, "last_id": None, "pending": True, "bad": False}
    return name


def _command(st: dict, bot: Bot, m: dict) -> None:
    owner = st["owner_id"]
    if "location" in m:
        loc = m["location"]
        st["home"] = [round(float(loc["latitude"]), 6), round(float(loc["longitude"]), 6)]
        bot.send(owner, "📍 Точка дома сохранена (в зашифрованном виде). Теперь «сколько идти» считаю от неё.")
        return
    origin = m.get("forward_origin") or {}
    fchat = origin.get("chat") if origin.get("type") == "channel" else m.get("forward_from_chat")
    if fchat:
        uname, title = fchat.get("username"), fchat.get("title") or "канал"
        if uname and add_channel(st, uname, title):
            bot.send(owner, f"👌 Принял «{title}» (@{uname}). Подключу при следующей проверке и пришлю лучшее за сутки.")
        elif uname:
            bot.send(owner, "Больше 50 каналов не потяну — убери лишние: /kanaly.")
        else:
            bot.send(owner, f"🔒 «{title}» — закрытый канал (без @имени). Без входа его не прочитать. "
                            "Такие подключаются только через отдельный пустой аккаунт — см. инструкцию, раздел «закрытые каналы».")
        return
    text = (m.get("text") or "").strip()
    if not text:
        return
    if not text.startswith("/"):
        refs = re.findall(r"(?:https?://)?t(?:elegram)?\.me/(?:s/)?[A-Za-z0-9_+]+|(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}", text)
        added = [n for n in (add_channel(st, r) for r in refs) if n]
        if added:
            bot.send(owner, "👌 Принял: " + ", ".join("@" + a for a in added) + ". Подключу при следующей проверке.")
        elif "t.me/+" in text or "joinchat" in text:
            bot.send(owner, "🔒 Это ссылка-приглашение в закрытый канал — без входа его не прочитать (см. инструкцию).")
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    if cmd in ("/start", "/help"):
        bot.send(owner, HELP)
    elif cmd == "/status":
        bot.send(owner, status_text(st, near_rate_of(st)))
    elif cmd == "/kanaly":
        chans = st.get("channels", {})
        if not chans:
            bot.send(owner, "Каналов пока нет. Перешли мне по посту из каждого канала с шабашками.")
        else:
            rows = [("❌ " if c.get("bad") else "⏳ " if c.get("pending") else "✅ ") + f"{c.get('title') or c['name']} (@{c['name']})"
                    for c in chans.values()]
            bot.send(owner, "<b>Каналы</b>\n" + "\n".join(rows) + "\n\n❌ — не читается (закрытый/группа), ⏳ — подключу при следующей проверке")
    elif cmd == "/ubrat":
        name = web_source.clean_name(arg)
        if name and st.get("channels", {}).pop(name.lower(), None):
            bot.send(owner, f"Ок, больше не читаю @{name}.")
        else:
            bot.send(owner, "Напиши так: <code>/ubrat имя_канала</code> (список — /kanaly).")
    elif cmd == "/stavka":
        try:
            val = int(arg)
            if not 200 <= val <= 5000:
                raise ValueError
            st["settings"]["near_rate"] = val
            bot.send(owner, f"Ок: «рядом» присылаю от <b>{val} ₽/ч</b>. Интересные — как и раньше, от {profile.INTEREST_RATE} ₽/ч.")
        except ValueError:
            bot.send(owner, f"Напиши число, например <code>/stavka 600</code>. Сейчас: {near_rate_of(st)} ₽/ч.")
    elif cmd in ("/pause", "/stop"):
        st["settings"]["paused"] = True
        bot.send(owner, "⏸ Пауза. Напиши /resume, когда снова надо.")
    elif cmd in ("/resume", "/go"):
        st["settings"]["paused"] = False
        bot.send(owner, "▶️ Снова слежу за шабашками.")
    elif cmd == "/papka":
        if arg:
            st["folder"] = arg[:64]
            bot.send(owner, f"Ок, папка «{arg[:64]}» (это только для режима закрытых чатов через отдельный аккаунт).")
        else:
            bot.send(owner, "Напиши так: <code>/papka Шабашки</code> — нужно только для закрытых чатов.")
    elif cmd == "/dom":
        bot.send(owner, DOM)


# ---------------------------------------------------------------------------
# Анализ пачки постов
# ---------------------------------------------------------------------------

def process_posts(posts: list[Post], st: dict, llm: LLM | None, geocoder: Geocoder | None,
                  home: tuple[float, float], now: float | None = None) -> list[tuple]:
    """Возвращает список (post, features, loc, verdict) для отправки."""
    now = now or time.time()
    seen = Seen(st.get("seen"))
    near_rate = near_rate_of(st)
    pre = []
    for p in sorted(posts, key=lambda x: x.date):
        st["stats"]["checked"] += 1
        if seen.is_dup(p.text):
            continue
        seen.add(p.text, p.date)
        f = analyze(p.text)
        if f.kind in DROP_KINDS:
            st["stats"]["dropped"] += 1
            continue
        t = norm(p.text)
        v0 = evaluate(f, zone_lookup(t) or Loc("не указано", None), near_rate=near_rate)
        if v0.drop_reason:
            st["stats"]["dropped"] += 1
            continue
        pre.append((p, f, t, v0))

    ai_map: dict[str, dict] = {}
    if llm is not None and llm.available:
        ask = [(str(i), p.text) for i, (p, f, t, v0) in enumerate(pre)
               if v0.send or v0.score >= LLM_MIN_PRESCORE or v0.interest >= 0.4]
        if ask:
            ai_map = llm.assess(ask)
            st["stats"]["llm"] += llm.calls
            llm.calls = 0

    fresh, backlog = [], []
    for i, (p, f, t, v0) in enumerate(pre):
        ai = ai_map.get(str(i))
        promising = v0.send or v0.score >= LLM_MIN_PRESCORE or ai is not None
        loc = locate(p.text, t, home, geocoder if promising else None, (ai or {}).get("place"))
        v = evaluate(f, loc, ai, near_rate=near_rate)
        if v.drop_reason:
            st["stats"]["dropped"] += 1
            continue
        if not v.send:
            continue
        age_h = (now - p.date) / 3600
        if age_h > (profile.FIRST_RUN_HOURS if p.backlog else MAX_AGE_HOURS):
            continue
        (backlog if p.backlog else fresh).append((p, f, loc, v))

    st["seen"] = seen.dump()
    if len(backlog) > profile.FIRST_RUN_MAX_CARDS:
        backlog = sorted(backlog, key=lambda x: (x[3].hot, x[3].score), reverse=True)[:profile.FIRST_RUN_MAX_CARDS]
    out = backlog + fresh
    out.sort(key=lambda x: x[0].date)
    return out


def send_cards(bot: Bot, st: dict, items: list[tuple], now: float | None = None) -> int:
    sent = 0
    for p, f, loc, v in items:
        text, buttons = card(p, f, loc, v, now)
        try:
            bot.send(st["owner_id"], text, buttons)
            sent += 1
            time.sleep(0.4)
        except BotError as e:
            log(f"бот: не отправил карточку ({str(e)[:60]})")
    st["stats"]["sent"] += sent
    return sent


def _home(cfg: Config, st: dict) -> tuple[float, float]:
    if st.get("home"):
        return tuple(st["home"])  # type: ignore[return-value]
    return cfg.home or geo.DEFAULT_HOME


# ---------------------------------------------------------------------------
# Один проход (GitHub Actions / cron) и бесконечный цикл (VPS / ПК)
# ---------------------------------------------------------------------------

def _save(cfg: Config, st: dict) -> None:
    state_mod.save(cfg.state_path, cfg.state_secrets[0], st)


def _web_posts(st: dict, notes: list[str]) -> tuple[list[Post], int]:
    """Публичные каналы через t.me/s — без входа в Telegram."""
    since = int(time.time() - profile.FIRST_RUN_HOURS * 3600)
    posts, failed = [], 0
    for key, ch in list(st.get("channels", {}).items()):
        if ch.get("bad"):
            continue
        try:
            got, max_id, title = web_source.fetch_new(ch["name"], ch.get("last_id"), since)
        except web_source.ChannelUnavailable:
            ch["bad"], ch["pending"] = True, False
            notes.append(f"⚠️ Не могу прочитать @{ch['name']}: это закрытый канал, группа или его нет. "
                         "Закрытые читаются только через отдельный пустой аккаунт (см. инструкцию).")
            continue
        except Exception as e:  # noqa: BLE001 — один канал не должен ронять остальные
            failed += 1
            log(f"канал пропущен: {type(e).__name__}")
            continue
        new_channel = ch.get("last_id") is None
        for p in got:
            p.backlog = new_channel
        if ch.get("pending"):
            notes.append(f"✅ Читаю «{title}» (@{ch['name']})" + (f" — за сутки там {len(got)} постов, лучшие пришлю." if got else "."))
            ch["pending"] = False
        ch["last_id"], ch["title"] = max_id, title
        posts += got
        time.sleep(0.5)
    return posts, failed


async def _account_posts(cfg: Config, st: dict, bot: Bot, client=None) -> tuple[list[Post], int]:
    """Закрытые чаты: только через ОТДЕЛЬНЫЙ аккаунт (режим по желанию)."""
    from .telegram_source import chat_info, fetch_new, folder_chats, make_client, peer_key

    own = client is None
    if own:
        client = make_client(st["session"], cfg.api_id, cfg.api_hash)
        await client.connect()
    posts, failed = [], 0
    try:
        if not await client.is_user_authorized():
            notify_once(st, bot, "auth", "⚠️ Вход отдельного аккаунта больше не действует — запусти «Шабашки: вход в Telegram» ещё раз.")
            return [], 0
        folder = st.get("folder") or cfg.folder
        if not folder:
            return [], 0
        since = int(time.time() - profile.FIRST_RUN_HOURS * 3600)
        chats_now = {}
        for ent in await folder_chats(client, folder):
            key = peer_key(ent)
            prev = st["chats"].get(key) or {}
            try:
                got, max_id = await fetch_new(client, ent, prev.get("last_id"), since)
            except Exception as e:  # noqa: BLE001
                failed += 1
                log(f"чат пропущен: {type(e).__name__}")
                if prev:
                    chats_now[key] = prev
                continue
            for p in got:
                p.backlog = not prev.get("last_id")
            info = chat_info(ent)
            chats_now[key] = {"last_id": max_id, "title": info["title"], "username": info["username"]}
            posts += got
        st["chats"] = chats_now
    finally:
        if own:
            await client.disconnect()
    return posts, failed


def _seed_channels(st: dict, names: list[str]) -> None:
    """Каналы по умолчанию и из секрета CHANNELS подключаем по одному разу.
    Если владелец убрал канал (/ubrat), он больше не вернётся сам."""
    done = set(st.get("seeded_names") or [])
    for raw in names:
        name = web_source.clean_name(raw)
        if name and name.lower() not in done:
            add_channel(st, name)
            done.add(name.lower())
    st["seeded_names"] = sorted(done)[:200]


async def run_once(cfg: Config, client=None) -> None:
    try:
        st = state_mod.load(cfg.state_path, cfg.state_secrets)
    except state_mod.StateError:
        if cfg.strong_key:
            raise                                    # свой STATE_KEY: ничего не перезаписываем (там может быть сессия)
        # ключ брался из токена бота, а токен сменили (/revoke). Ценного там нет — сессия без STATE_KEY не хранится.
        log("состояние от старого токена бота — начинаю с чистого листа")
        st = state_mod.empty_state()
    _roll_day(st)
    bot = Bot(cfg.bot_token)
    handle_commands(st, bot, cfg.owner_id)
    try:
        if not st.get("owner_id"):
            log("жду, пока владелец нажмёт Start у бота")
            return
        if st["settings"].get("paused"):
            log("пауза")
            return
        notes: list[str] = []
        _seed_channels(st, list(profile.DEFAULT_CHANNELS) + list(cfg.channels))
        posts, failed = _web_posts(st, notes)
        if st.get("session") and cfg.api_id and cfg.api_hash:
            more, f2 = await _account_posts(cfg, st, bot, client)
            posts += more
            failed += f2
        if not st.get("channels") and not st.get("session"):
            notify_once(st, bot, "nochan", "📥 Перешли мне по одному посту из каждого канала с шабашками "
                                            "(или пришли ссылки) — и я начну их читать.", every_h=24)
        llm = LLM(cfg.llm_token, cfg.llm_models, cap_left=cfg.llm_daily_cap - st["stats"].get("llm", 0))
        gc_cache = {k: v for k, v in st.get("geo", {}).items() if not v or time.time() - v[2] < 60 * 86400}
        geocoder = Geocoder(gc_cache, cfg.user_agent, enabled=cfg.geocode)
        items = process_posts(posts, st, llm, geocoder, _home(cfg, st))
        st["geo"] = dict(list(gc_cache.items())[-400:])
        for note in notes:
            try:
                bot.send(st["owner_id"], note)
            except BotError:
                pass
        sent = send_cards(bot, st, items)
        log(f"источников {len(st.get('channels', {}))} (ошибок {failed}), новых {len(posts)}, отправил {sent}, "
            f"нейросеть: {'ок' if llm.disabled_reason is None else llm.disabled_reason}")
    finally:
        st["last_run"] = int(time.time())
        _save(cfg, st)


async def run_loop(cfg: Config) -> None:
    """Для своего сервера/ПК: держим соединение и проверяем каждые LOOP_SECONDS (по умолчанию 60 с).
    Если связь с Telegram не восстанавливается — выходим с ошибкой, systemd перезапустит процесс."""
    from .telegram_source import make_client

    st = state_mod.load(cfg.state_path, cfg.state_secrets)
    client = None
    if st.get("session") and cfg.api_id and cfg.api_hash:
        client = make_client(st["session"], cfg.api_id, cfg.api_hash)
        await client.connect()
    log(f"цикл запущен, проверка каждые {cfg.loop_seconds} с")
    fails = 0
    try:
        while True:
            try:
                if client is not None and not client.is_connected():
                    await client.connect()
                await run_once(cfg, client=client)
                fails = 0
            except state_mod.StateError:
                raise
            except Exception as e:  # noqa: BLE001 — один сбой не должен убивать цикл
                fails += 1
                log(f"ошибка прохода: {type(e).__name__} ({fails} подряд)")
                if fails >= 5:
                    raise SystemExit(1)
            await asyncio.sleep(cfg.loop_seconds)
    finally:
        if client is not None:
            await client.disconnect()


# ---------------------------------------------------------------------------
# Вход в Telegram по QR (через твоего бота)
# ---------------------------------------------------------------------------

def _qr_png(url: str) -> bytes:
    import segno
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=10, border=4)
    return buf.getvalue()


def _find_owner_candidate(st: dict, bot: Bot) -> tuple[int | None, int]:
    """Кто нажал /start у бота. Возвращает (id, сколько разных людей нажали)."""
    senders: list[int] = []
    for u in bot.updates(offset=st.get("bot_offset", 0), timeout=0):
        m = u.get("message") or {}
        if (m.get("chat") or {}).get("type") == "private" and (m.get("text") or "").startswith("/start"):
            uid = (m.get("from") or {}).get("id")
            if uid and uid not in senders:
                senders.append(uid)
    return (senders[-1] if senders else None), len(senders)


async def _logout_old(cfg: Config, old_session: str | None) -> None:
    """При повторном входе закрываем старую авторизацию бота, чтобы не висела лишняя."""
    if not old_session:
        return
    from .telegram_source import make_client
    old = make_client(old_session, cfg.api_id, cfg.api_hash)
    try:
        await old.connect()
        if await old.is_user_authorized():
            await old.log_out()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            await old.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def run_login(cfg: Config) -> None:
    from telethon import errors as tg_errors

    from .telegram_source import (NeedPassword, folder_chats, folder_titles, list_folders, make_client, qr_login)

    if not (cfg.api_id and cfg.api_hash):
        log("Для закрытых чатов нужны секреты TG_API_ID и TG_API_HASH (my.telegram.org) отдельного аккаунта.")
        raise SystemExit(1)
    if not cfg.strong_key:
        log("Для входа обязателен секрет STATE_KEY (длинная случайная строка): им шифруется сессия.")
        raise SystemExit(1)
    try:
        st = state_mod.load(cfg.state_path, cfg.state_secrets)
    except state_mod.StateError:
        log("старое состояние не расшифровалось (менял секреты?) — начинаю с чистого листа")
        st = state_mod.empty_state()
    bot = Bot(cfg.bot_token)
    bot.me()  # проверка токена (имя бота в публичный лог не пишем)
    owner = cfg.owner_id or st.get("owner_id")
    if not owner:
        owner, n = _find_owner_candidate(st, bot)
        if n > 1:
            log("Боту написали /start несколько разных аккаунтов. Создай нового бота в @BotFather "
                "или добавь секрет OWNER_ID (твой числовой id) и запусти вход снова.")
            raise SystemExit(1)
    if not owner:
        log("Сначала открой своего бота в Telegram и нажми «Запустить» (/start), потом сразу запусти вход снова.")
        raise SystemExit(1)

    client = make_client(None, cfg.api_id, cfg.api_hash, updates=True)
    await client.connect()
    shown: list[int] = []

    async def show(url: str) -> None:
        for mid in shown:
            bot.delete(owner, mid)
        shown.clear()
        cap = ("🔑 <b>Вход для бота шабашек</b>\n\nНа телефоне: Telegram → Настройки → Устройства → "
               "<b>Подключить устройство</b> и наведи камеру на этот QR.\n\nQR обновляется примерно каждые 30 секунд. "
               "Открой это сообщение на ПК (Telegram Desktop), чтобы было что сканировать.")
        try:
            r = bot.send_photo(owner, _qr_png(url), cap)
            shown.append(r["message_id"])
        except BotError:
            log("не смог отправить QR в бота")

    try:
        user = await qr_login(client, show, cfg.tg_password)
    except NeedPassword:
        bot.send(owner, "🔐 У тебя включён облачный пароль Telegram. Добавь в GitHub секрет <code>TG_PASSWORD</code> "
                        "и запусти «Вход в Telegram» ещё раз. После входа секрет можно удалить.")
        await client.disconnect()
        raise SystemExit(1)
    except tg_errors.PasswordHashInvalidError:
        bot.send(owner, "🔐 Облачный пароль в секрете <code>TG_PASSWORD</code> не подошёл. Исправь его и запусти вход снова.")
        await client.disconnect()
        raise SystemExit(1)
    except TimeoutError:
        bot.send(owner, "⌛ QR не отсканировали за 5 минут. Запусти «Вход в Telegram» ещё раз.")
        await client.disconnect()
        raise SystemExit(1)
    finally:
        for mid in shown:
            bot.delete(owner, mid)

    if user.id != owner:
        # QR отсканировал не тот, кто нажал /start у бота — выходим и ничего не сохраняем
        await client.log_out()
        bot.send(owner, "⛔ QR отсканировал другой аккаунт. Вход отменён, ничего не сохранено. "
                        "Сканируй тем же аккаунтом, с которого нажимал /start у бота.")
        raise SystemExit(1)

    old_session = st.get("session")
    st["session"] = client.session.save()
    st["owner_id"] = user.id
    _save(cfg, st)                      # сразу сохраняем: дальше что угодно может сломаться
    await _logout_old(cfg, old_session)

    try:
        bot.set_commands()
        folders = folder_titles(await list_folders(client))
        chosen = cfg.folder or st.get("folder")
        guess = None
        if not chosen and folders:
            guess = next((n for n in folders if any(w in n.lower() for w in ("шаб", "подраб", "работ", "халтур", "ваканс"))), None)
            listing = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(folders))
            bot.send(owner, "✅ Вход выполнен!\n\nВ какой папке твои чаты с шабашками? Ответь <b>номером</b>:\n" + listing
                     + (f"\n\nЕсли не ответишь за 3 минуты — возьму «{guess}»." if guess else ""))
            deadline = time.time() + 180
            while time.time() < deadline and not chosen:
                for u in bot.updates(offset=st.get("bot_offset", 0), timeout=20):
                    st["bot_offset"] = u["update_id"] + 1
                    m = u.get("message") or {}
                    if (m.get("from") or {}).get("id") != owner:
                        continue
                    ans = (m.get("text") or "").strip()
                    if ans.isdigit() and 1 <= int(ans) <= len(folders):
                        chosen = folders[int(ans) - 1]
                    elif ans and not ans.startswith("/"):
                        chosen = next((n for n in folders if ans.lower() in n.lower()), None)
            chosen = chosen or guess
        st["folder"] = chosen
        n_chats = len(await folder_chats(client, chosen)) if chosen else 0
    finally:
        await client.disconnect()
        _save(cfg, st)
    if chosen:
        bot.send(owner, f"🎯 Папка «{chosen}»: нашёл {n_chats} чатов. Первая проверка — в ближайшие минуты, "
                        "дальше присылаю пиздатые шабашки, как только появляются.\n\n" + DOM + "\n\n/help — команды")
    elif folders:
        bot.send(owner, "📂 Папку не выбрал. Напиши боту <code>/papka Название</code> — твои папки: "
                        + ", ".join(f"«{n}»" for n in folders))
    else:
        bot.send(owner, "📂 Папок в Telegram не нашёл. Создай папку с чатами шабашек и напиши боту "
                        "<code>/papka Название</code>.")
    log("вход выполнен")
