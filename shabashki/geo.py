"""Где работа и сколько до неё идти от дома.

Два уровня:
1) Точный: если в тексте есть адрес (улица + дом), геокодим через OpenStreetMap
   (Nominatim, бесплатно, с кэшем и паузой 1 с между запросами — по их правилам).
2) Примерный: словарь районов/улиц/мест Владивостока с оценкой минут пешком
   от центральной площади. Работает без интернета.
Точку дома задаёшь геопозицией в боте (хранится зашифрованной) или секретом HOME_COORDS.
"""
from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import profile

# Запасная точка «дом», если не задана: центральная площадь Владивостока.
DEFAULT_HOME = (43.1155, 131.8855)

# Рамка Владивостока (вкл. Русский и Садгород) — чтобы геокодер не уехал в другой город
VLAD_BOX = (131.75, 43.30, 132.20, 42.95)  # lon_min, lat_max, lon_max, lat_min


def walk_minutes(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 2 * r * math.asin(math.sqrt(a))
    return max(1, int(round(km * profile.ROUTE_FACTOR / profile.WALK_SPEED_KMH * 60)))


# ---------------------------------------------------------------------------
# Словарь мест (минуты пешком от центральной площади, оценка)
# ---------------------------------------------------------------------------
OUT = 999  # другой город

_CENTRE_WORD = (r"(?<!торговый )(?<!торгового )(?<!бизнес )(?<!бизнес-)(?<!сервисный )(?<!учебный )"
                r"(?<!медицинский )(?<!фитнес )(?<!фитнес-)(?<!выставочный )\bцентр(?:е|а|у)?\b(?!\s+(?:занятост|артем|артём|уссурийск|находк))")

PLACES: list[tuple[str, str, int]] = [
    # --- рядом (до ~15 мин) ---
    (r"burny|бурн(?:ый|ом|ого)\b|rodina\s+residences|родина\s+резиденс|uliss|улисс", "BURNY (Набережная 13)", 13),
    (r"grand\s+hotel|гранд\s+отел|rodina\s+grand|корабельн\w+\s+наб\w*,?\s*(?:д\.?\s*)?10\b", "Grand Hotel & Spa", 12),
    (r"7\s*фут|семь\s+фут", "яхт-клуб «7 Футов»", 13),
    (r"\bzuma\b|\bзума\b", "Zuma", 8),
    (r"\bгусто\b|gusto", "Густо (ГУМ)", 4),
    (r"набережн(?!\w*\s+цесаревич)", "Набережная", 14),
    (r"спортивн\w*\s+(?:гаван|набережн)|спортивк", "Спортивная гавань", 13),
    (r"кинотеатр\w*\s+океан|\bокеан\b(?!\w*\s*(?:пр\b|пр\.|проспект|ский))", "к/т Океан", 13),
    (r"азимут|azimut", "Azimut", 15),
    (r"экватор", "отель Экватор", 16),
    (r"\bарбат|адмирала\s+фокина|\bфокина\b", "Арбат/Фокина", 6),
    (r"алеутск", "Алеутская", 7),
    (r"посьетск", "Посьетская", 11),
    (r"(?:ж/?д|жд)\.?\s*вокзал|\bвокзал|морвокзал|морск\w+\s+вокзал|паромн", "вокзал", 9),
    (r"пограничн", "Пограничная", 10),
    (r"тигров", "Тигровая", 10),
    (r"динамо", "стадион Динамо", 13),
    (r"(?:1|перв\w*)-?(?:я|ая)?\s+морск|(?:2|втор\w*)-?(?:я|ая)?\s+морск", "Морская", 9),
    (r"миллионк", "Миллионка", 7),
    (r"версаль|versailles", "отель Версаль", 9),
    (r"светланск", "Светланская", 8),
    (r"батарейн", "Батарейная", 14),
    (r"жемчужин", "отель Жемчужина", 15),
    (r"центральн\w+\s+площад|площад\w+\s+борцов", "центр. площадь", 3),
    (r"\bцум(?:а|е|у|ом)?\b|\bгум(?:а|е|у|ом)?\b|клевер|clover", "ЦУМ/ГУМ/Clover", 4),
    (r"филармон|пушкинск\w+\s+театр", "филармония", 8),
    (r"семеновск", "Семёновская", 8),
    (_CENTRE_WORD, "центр", 10),
    (r"фонтанн", "Фонтанная", 8),
    (r"уборевич", "Уборевича", 8),
    (r"мордовцев", "Мордовцева", 9),
    (r"лотте|lotte|х[её]нда|hyundai", "Lotte", 12),
    (r"крепост", "крепость", 14),
    (r"петра\s+великого", "Петра Великого", 10),
    (r"феско\s*холл|fesco\s*hall", "Fesco Hall", 12),
    (r"океанск\w*\s*(?:пр\b|пр-т|пр\.|проспект)|океанский", "Океанский пр-т", 10),
    (r"лазо\b", "Лазо", 10),
    (r"суханов", "Суханова", 12),
    (r"пушкинск(?!\w+\s+театр)", "Пушкинская", 12),
    # --- далековато (20–45 мин) ---
    (r"драм\w*\s*театр|театр\w*\s+(?:им\.?\s+|имени\s+)?горьк|горьковск", "театр Горького", 9),
    (r"с-?56|подлодк|подводн\w+\s+лодк|цесаревич|корабельн\w+\s+набережн", "наб. Цесаревича", 12),
    (r"фуникул|орлин\w+\s+гнезд", "фуникулёр", 15),
    (r"\bцирк", "цирк", 25),
    (r"покровск\w+\s+парк|покровк", "Покровский парк", 22),
    (r"перв\w*\s+речк|1-?я\s+речк", "Первая Речка", 35),
    (r"эгершельд|верхнепортов|крыгин|жигур", "Эгершельд", 35),
    (r"гоголя|вгуэс|некрасовск|\bбам\b|партизанск\w*\s*(?:пр\b|пр-т|пр\.|проспект)|народн\w*\s*(?:пр\b|пр-т|пр\.|проспект)", "Гоголя/Некрасовская", 30),
    (r"дальзавод|гнил\w+\s+угол|шилкинск|капитана\s+шефнера", "Дальзавод/Гнилой угол", 35),
    (r"луговая|луговой|луговую", "Луговая", 45),
    (r"токаревск", "Токаревский маяк", 55),
    # --- далеко (нужен транспорт) ---
    (r"втор\w*\s+речк|2-?я\s+речк", "Вторая Речка", 75),
    (r"чуркин|гайдамак|калинина|мариинск|приморск\w+\s+сцен", "Чуркин", 65),
    (r"\bтих(?:ая|ой|ую)\b|баляев|\bзаря\b|на\s+заре|моргородок|снегов\w*\s+пад|патрокл|сабанеев|столетия|100-?летия|бородинск|маковского|\bлесн(?:ая|ой)\b|русск(?:ая|ой)\s+(?:ул|д\.|\d)", "спальные районы", 80),
    (r"русск\w*\s+остров|остров\w*\s+русск|на\s+русском\b|двфу|дфву|\bфефу\b|аякс|океанариум", "о. Русский", 90),
    (r"садгород|седанк|санаторн|трудов(?:ое|ом|ого)\b|углов(?:ое|ом|ого)\b|де-?фриз|шамор|мингородок|девят\w+\s+вал|ботаническ", "пригород", 110),
    (r"аэропорт|кневич|\bартем\b|\bартём\b|надеждинск|\bновый\b(?=\s|,|$)|муравьин|тигр\s+де|tigre|шамбала|selena", "Артём/аэропорт", 150),
    # --- другие города ---
    (r"уссурийск|находк|хабаровск|большо\w+\s+камн|большой\s+камень|партизанск\b|\bфокино\b|спасск|лесозаводск|"
     r"(?<!музей )(?<!музея )\bарсеньев\b|славянк|зарубино|\bпосьет\b|дальнегорск|кавалерово|лучегорск|благовещенск|новосибирск|москв|иркутск|сахалин", "другой город", OUT),
]

# Длинные улицы: минуты зависят от номера дома
# Номер дома, а не деньги и не время: «Светланская 205» — да; «Светланская, 500 р/ч», «…, 3000 за смену», «14:00» — нет.
HOUSE = (r"(\d{1,3})(?!\d)(?![:.,]\d)(?!\s*-\s*\d)(?!\s\d{3}\b)"
         r"(?!\s*(?i:р\b|р\.|р/|₽|руб|тыс|т\.?\s?р|к\b|k\b|за\b|в\s+час|/\s*ч|ч\b|час|чел|человек|мин|шт|%|-?(?:й|я|го|е)\b))")
_NUM = r",?\s*(?:д\.?\s*|дом\s*)?" + HOUSE
NUM_RULES: dict[str, tuple[str, list[tuple[int, int]]]] = {
    "Светланская": (r"светланск\w*" + _NUM, [(60, 8), (110, 20), (160, 35), (999, 50)]),
    "Океанский пр-т": (r"океанск\w*\s*(?:пр\b|пр-т|пр\.|проспект)?" + _NUM, [(20, 8), (50, 18), (100, 30), (999, 50)]),
    "Алеутская": (r"алеутск\w*" + _NUM, [(30, 7), (999, 15)]),
    "Арбат/Фокина": (r"фокина" + _NUM, [(30, 6), (999, 10)]),
    "Набережная": (r"набережн\w*" + _NUM, [(20, 13), (999, 16)]),
    "Семёновская": (r"семеновск\w*" + _NUM, [(30, 8), (999, 12)]),
}


@dataclass
class Loc:
    label: str
    minutes: int | None      # None — непонятно где
    precise: bool = False    # True — посчитано по координатам адреса
    query: str | None = None
    lat: float | None = None
    lon: float | None = None

    @property
    def zone(self) -> str:
        m = self.minutes
        if m is None:
            return "unknown"
        if m >= OUT:
            return "out"
        if m <= profile.WALK_IDEAL_MIN:
            return "near"
        if m <= profile.WALK_MAX_MIN:
            return "ok"
        if m <= 45:
            return "mid"
        return "far"


def zone_lookup(t: str) -> Loc | None:
    """Примерная оценка по словарю. t — нормализованный текст (lower, ё→е)."""
    best: Loc | None = None
    # «торговый центр», «бизнес-центр» и т.п. — это не «центр города»
    t = re.sub(r"(?:торгов\w*|бизнес-?|сервисн\w*|учебн\w*|медицинск\w*|фитнес-?|выставочн\w*|развлекательн\w*|"
               r"культурн\w*|делов\w*|логистическ\w*|распределительн\w*|офисн\w*|спортивн\w*|детск\w*)\s*центр\w*|\bтц\b|\bтрц\b|\bбц\b", " ", t)
    for pat, label, minutes in PLACES:
        m = re.search(pat, t)
        if not m:
            continue
        mins = minutes
        rule = NUM_RULES.get(label)
        if rule:
            nm = re.search(rule[0], t)
            if nm:
                n = int(nm.group(1))
                for limit, val in rule[1]:
                    if n <= limit:
                        mins = val
                        break
        cand = Loc(label=label, minutes=mins)
        # приоритет — ближайшее место в городе (точка сбора), «другой город» — только если больше ничего
        if best is None or (best.minutes == OUT and mins != OUT) or (mins != OUT and mins < (best.minutes or OUT)):
            best = cand
    return best


# ---------------------------------------------------------------------------
# Адреса для точного геокодинга
# ---------------------------------------------------------------------------
_ADDR = re.compile(
    r"(?:(?:ул\.?|улица|пр-т|пр\.|проспект|пер\.?|переулок|наб\.?|набережная|бульвар|б-р|шоссе)\s+)?"
    r"((?:[А-ЯЁ][а-яё]+(?:-[А-ЯЁа-яё][а-яё]+)?)(?:\s+[А-ЯЁ][а-яё]+)?|\d-?я\s+[А-ЯЁ][а-яё]+|Адмирала\s+[А-ЯЁ][а-яё]+|Петра\s+Великого|Капитана\s+[А-ЯЁ][а-яё]+)"
    r"(?:\s+(?:проспект|пр-т|пр\.|улица|ул\.?|переулок|пер\.|набережная|наб\.?))?"
    r",?\s*(?:д\.?\s*|дом\s*)?((?:" + HOUSE.replace("(\\d{1,3})", "\\d{1,3}", 1) + r")[А-Яа-яA-Za-z]?(?:/\d{1,3})?)\b"
)
_STREET_HINT = re.compile(r"(?:ул\.?|улица|пр-т|проспект|пер\.?|переулок|наб\.?|набережная|бульвар|шоссе)\s+[А-ЯЁ]", re.I)
_NOT_STREET = {"Оплата", "Нужно", "Нужен", "Нужна", "Нужны", "Требуется", "Требуются", "Звоните", "Пишите", "Работа",
               "Смена", "Возраст", "Часов", "Часа", "Человек", "Время", "Сбор", "Начало", "Конец", "Дом", "Владивосток",
               "Офис", "Этаж", "Подъезд", "Квартира", "Кв", "Тел", "Телефон", "Итого", "Всего", "Плачу", "Заплачу", "От", "До",
               "Завтра", "Сегодня", "Ставка", "Шабашка", "Грузчики", "Грузчик", "Отель", "Ресторан", "Сопка", "Мыс"}


def address_candidates(text: str) -> list[str]:
    out = []
    for m in _ADDR.finditer(text or ""):
        name, num = m.group(1).strip(), m.group(2)
        first = name.split()[0]
        if first in _NOT_STREET:
            continue
        has_hint = bool(_STREET_HINT.match(m.group(0)))
        known = zone_lookup(name.lower().replace("ё", "е")) is not None
        if not (has_hint or known):
            continue
        q = re.sub(r"\s+", " ", m.group(0)).strip(" ,.")
        if q not in out:
            out.append(q)
    return out[:3]


class Geocoder:
    """Nominatim с кэшем. Кэш живёт в зашифрованном состоянии бота."""

    URL = "https://nominatim.openstreetmap.org/search"

    def __init__(self, cache: dict, user_agent: str, enabled: bool = True, max_calls: int = 6):
        self.cache = cache
        self.ua = user_agent
        self.enabled = enabled
        self.max_calls = max_calls
        self.calls = 0
        self._last = 0.0

    def geocode(self, query: str) -> tuple[float, float] | None:
        key = query.strip().lower()
        if key in self.cache:
            v = self.cache[key]
            return (v[0], v[1]) if v else None
        if not self.enabled or self.calls >= self.max_calls:
            return None
        wait = 1.1 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self.calls += 1
        self._last = time.time()
        params = {
            "q": f"{query}, Владивосток", "format": "jsonv2", "limit": "1", "countrycodes": "ru",
            "viewbox": ",".join(str(x) for x in VLAD_BOX), "bounded": "1", "accept-language": "ru",
        }
        req = urllib.request.Request(self.URL + "?" + urllib.parse.urlencode(params), headers={"User-Agent": self.ua})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            return None  # сеть/лимит — просто без точной координаты, в кэш не пишем
        res = None
        if data:
            try:
                lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
                if VLAD_BOX[3] <= lat <= VLAD_BOX[1] and VLAD_BOX[0] <= lon <= VLAD_BOX[2]:
                    res = (lat, lon)
            except (KeyError, ValueError, TypeError):
                res = None
        self.cache[key] = [res[0], res[1], int(time.time())] if res else []
        return res


def locate(raw_text: str, norm_text: str, home: tuple[float, float], geocoder: Geocoder | None,
           llm_place: str | None = None) -> Loc:
    approx = zone_lookup(norm_text)
    queries = address_candidates(raw_text)
    if llm_place and re.search(r"\d", llm_place) and llm_place not in queries:
        queries.append(llm_place)
    if geocoder:
        for q in queries:
            pt = geocoder.geocode(q)
            if pt:
                mins = walk_minutes(home[0], home[1], pt[0], pt[1])
                return Loc(label=q, minutes=mins, precise=True, query=q, lat=pt[0], lon=pt[1])
    if approx:
        return approx
    if llm_place:
        guess = zone_lookup(llm_place.lower().replace("ё", "е"))
        if guess:
            return guess
        return Loc(label=llm_place[:40], minutes=None)
    return Loc(label="не указано", minutes=None)
