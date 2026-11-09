"""Запуск:  python -m shabashki poll | login | loop | demo

poll  — один проход (для GitHub Actions по расписанию)
login — вход в Telegram по QR через бота (один раз)
loop  — бесконечный цикл с проверкой раз в минуту (для своего сервера/ПК)
demo  — показать, как бот оценит примеры объявлений (без интернета)
"""
from __future__ import annotations

import asyncio
import sys


def _demo() -> None:
    import time

    from .extract import analyze, norm
    from .geo import DEFAULT_HOME, locate
    from .notify import Post, card
    from .scoring import ROUTE_TITLES, evaluate
    from .samples import SAMPLES

    now = time.time()
    wrong = 0
    for i, (text, expect) in enumerate(SAMPLES):
        f = analyze(text)
        loc = locate(text, norm(text), DEFAULT_HOME, None)
        v = evaluate(f, loc)
        if v.send:
            mark = ("🔥 " if v.hot else "") + ROUTE_TITLES.get(v.route or "", "?")
        else:
            mark = "отсев: " + (v.drop_reason or v.skip_reason or "?")
        bad = expect != "any" and (expect == "send") != v.send
        wrong += bad
        print(f"\n#{i + 1:02d} [{v.score:3d}] {mark}{'   ❌ ожидалось: ' + expect if bad else ''}  интерес={v.interest} {v.parts}")
        print("    " + text[:110].replace("\n", " "))
        if v.send:
            html, _ = card(Post("w:demo", "Шабашки ВЛ", "demo_chat", 0, i + 1, int(now - 300), text), f, loc, v, now)
            print("    ---\n    " + html.split("\n\n<blockquote")[0].replace("\n", "\n    "))
    print(f"\nИтого примеров: {len(SAMPLES)}, расхождений с ожиданием: {wrong}")


def main() -> None:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "poll").lower()
    if mode == "demo":
        _demo()
        return
    from .config import Config, ConfigError
    try:
        cfg = Config.load()
    except ConfigError as e:
        print(f"Ошибка настроек: {e}")
        # расписание не валим (иначе GitHub засыплет письмами), а вход — валим, чтобы было видно
        sys.exit(0 if mode == "poll" else 2)
    from . import runner
    if mode == "login":
        asyncio.run(runner.run_login(cfg))
    elif mode == "loop":
        asyncio.run(runner.run_loop(cfg))
    elif mode == "poll":
        from . import state as st_mod
        try:
            asyncio.run(runner.run_once(cfg))
        except st_mod.StateError as e:
            # Состояние есть, но не расшифровывается (сменили BOT_TOKEN/STATE_KEY?). Ничего не перезаписываем.
            runner.log(f"состояние не расшифровано: {e}. Верни прежний STATE_KEY или удали ветку state — бот начнёт заново.")
            if cfg.owner_id:
                try:
                    from .botapi import Bot
                    Bot(cfg.bot_token).send(cfg.owner_id, "⚠️ Не могу расшифровать состояние бота (менял STATE_KEY?). "
                                            "Верни прежний секрет или удали в GitHub ветку state — начну с чистого листа.")
                except Exception:  # noqa: BLE001
                    pass
            sys.exit(1)
        except Exception as e:  # noqa: BLE001
            # Не роняем workflow из-за временной ошибки (иначе GitHub засыплет письмами) — сообщим в бота
            runner.log(f"ошибка: {type(e).__name__}")
            try:
                from .botapi import Bot
                st = st_mod.load(cfg.state_path, cfg.state_secrets)
                runner.notify_once(st, Bot(cfg.bot_token), "crash:" + type(e).__name__,
                                   f"⚠️ Проверка упала с ошибкой <code>{type(e).__name__}</code>. "
                                   "Если повторяется — загляни в GitHub → Actions.", every_h=12)
                st_mod.save(cfg.state_path, cfg.state_secrets[0], st)
            except Exception:  # noqa: BLE001
                pass
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
