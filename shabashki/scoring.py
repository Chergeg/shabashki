"""Решение «присылать или нет» (см. profile.py), по приоритету:

  🏨 ДОРОГОЕ МЕСТО — отель/ДВФУ/ВЭФ/топ-ресторан/яхт-клуб… + не физуха + от 400 ₽/ч или без цены
  🚚 ПОЕЗДКА       — погрузили → едешь в другой город → разгрузил → обратно
  ✨ ИНТЕРЕСНОЕ    — съёмки, массовка, большие ивенты, рилсы (интерес ≥ 0.8)
  📍 РЯДОМ         — до 30 мин пешком + от 500 ₽/ч; обычная погрузка ок, но не фуры/стройка,
                     не смены 8+ часов и не частник «на час-два»
  иначе — не присылаем.

Балл 0–100 нужен для сортировки и пометки 🔥: деньги 35 + лайтово 25 + интерес 25 + близко 15.
Нейросеть (если есть) уточняет ₽/ч, нагрузку, интерес — решение всегда принимает этот код.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import profile
from .extract import Features
from .geo import Loc, OUT

DROP_KINDS = {"scam", "closed", "seeker", "service", "unpaid", "chatter"}
VENUE_LABELS = {"дорогой отель", "дорогое место", "топовый ресторан"}
ROUTE_TITLES = {"venue": "🏨 ДОРОГОЕ МЕСТО", "trip": "🚚 ПОЕЗДКА", "interesting": "✨ ИНТЕРЕСНОЕ", "near": "📍 РЯДОМ"}


@dataclass
class Verdict:
    score: int
    send: bool
    hot: bool
    route: str | None = None          # "venue" | "trip" | "interesting" | "near" | None
    drop_reason: str | None = None
    hourly: int | None = None
    interest: float = 0.0
    pay_text: str = ""
    tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parts: dict = field(default_factory=dict)
    job_label: str = ""
    why: str = ""
    skip_reason: str | None = None    # почему не прислали (для отладки и демо)


def _hourly_points(h: int) -> int:
    pts = 0
    for lim, val in [(300, 4), (400, 12), (500, 20), (600, 26), (700, 30), (800, 33), (1000, 35)]:
        if h >= lim:
            pts = val
    return pts


def _pay_ok(rate: int, hourly, per_day, total, total_kind) -> bool | None:
    """True/False — хватает ли денег; None — оплата не указана."""
    if hourly:
        return hourly >= rate
    if per_day:
        return per_day >= rate * 7             # день ≈ 7 рабочих часов
    if total and total_kind in ("shift", "day"):
        return total >= rate * 7
    if total:
        return total >= rate * 3               # сумма без длительности — считаем, что часа три
    return None


def evaluate(f: Features, loc: Loc, ai: dict | None = None, near_rate: int | None = None) -> Verdict:
    ai = ai or {}
    near_rate = near_rate or profile.NEAR_RATE
    v = Verdict(score=0, send=False, hot=False)

    # --- отсев мусора и разводов ---
    if ai.get("scam") is True:
        v.drop_reason = "похоже на развод: " + str(ai.get("scam_reason") or "нейросеть")
        return v
    if f.kind in DROP_KINDS:
        v.drop_reason = f.kind_reason or f.kind
        return v
    if ai and ai.get("is_offer") is False and f.kind != "offer":
        v.drop_reason = "не предложение работы"
        return v
    if f.kind == "unknown" and not (f.jobs or f.pay.per_hour or f.pay.total) and not ai:
        v.drop_reason = "не похоже на шабашку"
        return v
    if f.permanent:
        v.drop_reason = "постоянная работа/вахта, а не шабашка"
        return v

    # --- деньги ---
    hourly = f.pay.effective_hourly
    ai_hourly = ai.get("pay_per_hour_rub")
    if not hourly and isinstance(ai_hourly, (int, float)) and 100 <= ai_hourly <= 10000:
        hourly = int(ai_hourly)
    per_day, total, tkind = f.pay.per_day, f.pay.total, f.pay.total_kind
    v.hourly = hourly

    # --- лайтовость (0 — физуха … 4 — почти ничего не делать) ---
    level = f.chill
    effort, idle = ai.get("effort"), ai.get("idle_share")
    if isinstance(effort, (int, float)) and 1 <= effort <= 5:
        ai_level = int(round(4 - (effort - 1)))
        if isinstance(idle, (int, float)) and idle >= 0.6:
            ai_level = min(4, ai_level + 1)
        level = ai_level if level is None else int(round((level + 2 * ai_level) / 3))

    # --- интерес 0..1 ---
    trip = f.trip or ai.get("trip") is True
    interest = max([lvl for _, lvl in f.interest] + [0.0])
    if trip:
        interest = max(interest, 0.7)
    if f.content and profile.CONTENT_CREATOR:
        interest = max(interest, 0.8)
        v.tags.append("🎬 по твоему профилю")
    ai_int = ai.get("interesting")
    if isinstance(ai_int, (int, float)):
        interest = max(interest, float(ai_int))
    v.interest = round(interest, 2)

    # --- где ---
    mins = loc.minutes
    near = mins is not None and mins < OUT and mins <= profile.NEAR_MAX_MIN
    other_city = mins is not None and mins >= OUT

    # --- требования ---
    blocked = False
    reqs = list(dict.fromkeys(f.reqs + [r for r in (ai.get("needs") or []) if isinstance(r, str)]))
    for r in reqs:
        rl = r.lower()
        if "прав" in rl:
            blocked |= profile.HAS_LICENSE is False
            v.warnings.append("нужны права")
        elif "авто" in rl or "машин" in rl:
            blocked |= profile.HAS_CAR is False
            v.warnings.append("нужно своё авто")
        elif "медкниж" in rl:
            blocked |= profile.HAS_MEDBOOK is False
            v.warnings.append("нужна медкнижка")
        elif "опыт" in rl:
            v.warnings.append("просят опыт")
        elif "одежд" in rl or "форм" in rl or "дресс" in rl:
            v.warnings.append("дресс-код")

    # --- маршрут: дорогое место → поездка → интересное → рядом ---
    venue = any(label in VENUE_LABELS for label, _ in f.interest) or ai.get("premium_place") is True
    if venue:
        interest = max(interest, 1.0)
        v.interest = 1.0
    ai_hours = ai.get("hours")
    short = f.short or (isinstance(ai_hours, (int, float)) and ai_hours <= 2)
    private = f.private or ai.get("private") is True
    # «частные лица и какие-нибудь на час, на два — хуетень: делать много, платят мало»
    quick_grind = short and ((private and (level is None or level <= 2)) or (level is not None and level <= 1))
    heavy = level == 0
    long_physical = level is not None and level <= 1 and (f.pay.hours or 0) >= 8
    near_pay = _pay_ok(near_rate, hourly, per_day, total, tkind)
    int_pay = _pay_ok(profile.INTEREST_RATE, hourly, per_day, total, tkind)
    light_enough = level is None or level >= 2
    if blocked:
        v.skip_reason = "нет нужных прав/авто/медкнижки"
    elif heavy:
        v.skip_reason = "тяжёлая физуха"
    elif venue and light_enough and int_pay is not False and not other_city:
        v.route = "venue"
    elif trip and (level is None or level >= 1) and int_pay is not False:
        v.route = "trip"
    elif interest >= profile.INTEREST_LEVEL and light_enough and int_pay is not False and not other_city:
        v.route = "interesting"
    elif near and quick_grind:
        v.skip_reason = ("частник" if private else "физуха") + " на час-два — работы много, денег мало"
    elif near and long_physical:
        v.skip_reason = "физуха на 8+ часов"
    elif near and (near_pay is True or (near_pay is None and level is not None and level >= 3)):
        v.route = "near"
    elif near and near_pay is False:
        v.skip_reason = f"рядом, но меньше {near_rate} ₽/ч"
    elif not near:
        v.skip_reason = "далеко и не особо интересно"
    else:
        v.skip_reason = "оплата не указана и не лайтово"

    # --- балл для сортировки ---
    if hourly:
        pay_pts = _hourly_points(hourly)
    elif per_day or (total and tkind in ("shift", "day")):
        day = per_day or total
        pay_pts = 30 if day >= 6000 else 25 if day >= 4500 else 18 if day >= 3000 else 8
    elif total:
        pay_pts = 16 if total >= 3000 else 12 if total >= 1500 else 6
    else:
        pay_pts = 10
    chill_pts = {4: 25, 3: 20, 2: 12, 1: 6, 0: 0}.get(level, 10) if level is not None else 10
    int_pts = int(round(25 * interest))
    if trip:
        near_pts = 8
    elif mins is None:
        near_pts = 4
    else:
        near_pts = 15 if mins <= 10 else 13 if mins <= 15 else 10 if mins <= 20 else 5 if mins <= 30 else 0
    bonus = 0
    if f.urgent:
        bonus += 3
        v.tags.append("⚡ срочно")
    if f.pay_now:
        bonus += 2
        v.tags.append("💵 оплата сразу")
    v.score = max(0, min(100, pay_pts + chill_pts + int_pts + near_pts + bonus))
    v.parts = {"деньги": pay_pts, "лайтово": chill_pts, "интерес": int_pts, "близко": near_pts, "бонус": bonus}

    # --- итог и теги ---
    v.send = v.route is not None
    v.hot = v.send and (v.route in ("venue", "trip") or v.score >= profile.HOT_SCORE)
    if hourly and hourly >= profile.GOOD_RATE:
        v.tags.insert(0, "💸 дорого")
    if level is not None and level >= 3 and not trip:
        v.tags.insert(0, "😴 лайтово")
    if trip:
        v.tags.insert(0, "🚚 в основном едешь")
    elif near:
        v.tags.insert(0, "📍 рядом")
    if interest >= profile.INTEREST_LEVEL and v.route != "venue":
        v.tags.insert(0, "✨ интересно")
    if level is not None and level <= 1:
        v.warnings.append("физическая работа")
    if near_pay is None and int_pay is None:
        v.warnings.append("оплата не указана — уточни")

    v.job_label = ai.get("job") if isinstance(ai.get("job"), str) and ai.get("job") else _job_label(f, v.route)
    v.why = ai.get("why") if isinstance(ai.get("why"), str) else ""
    v.pay_text = _pay_text(f, hourly)
    return v


def _job_label(f: Features, route: str | None) -> str:
    keys = {k for k, _, _ in f.jobs}
    if route == "trip":
        return "поездка с погрузкой" if keys & {"movers", "warehouse"} else "поездка/сопровождение"
    core = [label for k, label, _ in f.jobs if k != "event"]   # «официант на банкете» → официант
    return (core or [label for _, label, _ in f.jobs] or ["шабашка"])[0]


def _pay_text(f: Features, hourly: int | None) -> str:
    p = f.pay
    if p.per_hour:
        return f"{p.per_hour}–{p.per_hour_max} ₽/ч" if p.per_hour_max else f"{p.per_hour} ₽/ч"
    if p.total and hourly and p.hours:
        return f"~{hourly} ₽/ч ({p.total:,} ₽ за {p.hours:g} ч)".replace(",", " ")
    if p.total and p.per_day and p.days:
        return f"{p.total:,} ₽ за {p.days:g} дн. (~{p.per_day:,} ₽/день)".replace(",", " ")
    if p.total:
        kind = {"shift": " за смену", "day": " в день"}.get(p.total_kind or "", "")
        s = f"{p.total:,} ₽{kind}".replace(",", " ")
        return s + (f" (~{hourly} ₽/ч — оценка)" if hourly else "")
    if hourly:
        return f"~{hourly} ₽/ч (оценка)"
    return "не указана"
