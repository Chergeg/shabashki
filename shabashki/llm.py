"""Нейросеть-оценщик через GitHub Models (бесплатно, токен GITHUB_TOKEN из Actions).

Нейросеть НЕ ставит балл — она только вытаскивает признаки, которые сложно
поймать регулярками: сколько реально выходит в час, насколько тяжело,
сколько времени «тупишь», это развод или нет. Балл считает scoring.py.
Отправляем текст объявления БЕЗ телефонов, @ников и ссылок, плюс описание вкусов (без адреса).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import profile

ENDPOINT = "https://models.github.ai/inference/chat/completions"

SYSTEM = (
    "Ты — придирчивый фильтр подработок (шабашек) во Владивостоке для одного человека.\n"
    f"О нём: {profile.PERSON_BRIEF}\n\n"
    "Для каждого объявления верни ОБЪЕКТИВНЫЕ признаки. Ничего не выдумывай: если в тексте нет данных — null.\n"
    "Поля каждого элемента:\n"
    "id — как во входе;\n"
    "is_offer — true, если это предложение работы/подработки исполнителю (а не человек ищет работу, не реклама услуг, не продажа, не болтовня);\n"
    "scam — true, если похоже на развод/криминал (дропы и карты на себя, курьер наличных/документов, закладки, «доход без вложений», «ставь + в лс», просят заплатить им); scam_reason — коротко;\n"
    "job — что делать, 2–6 слов по-русски, с маленькой буквы;\n"
    "pay_per_hour_rub — сколько выходит в час (сумма ÷ реальные часы), число или null;\n"
    "effort — 1..5: 1 почти ничего не делать (сидеть, стоять, ехать), 2 легко, 3 средне (официант, промо), 4 тяжело (грузчик), 5 очень тяжело (фуры, стройка, склад весь день);\n"
    "idle_share — 0..1, доля времени, когда можно ничего не делать (ожидание, дорога);\n"
    "trip — true, если основная часть — поездка (сопровождение груза, экспедитор, перегон, выезд в другой город);\n"
    "place — где (адрес, район или место как в тексте), коротко, или null;\n"
    "interesting — 0..1: насколько круто/необычно (отель уровня Lotte/Azimut, VIP, банкет, ивент, съёмки, яхта = высоко; обычный склад = 0);\n"
    "needs — список требований из: \"права\", \"своё авто\", \"медкнижка\", \"опыт\", \"дресс-код\";\n"
    "premium_place — true, если работа в дорогом месте: дорогой отель, ДВФУ, ВЭФ, топовый ресторан, яхт-клуб, казино;\n"
    "private — true, если заказчик частное лицо с разовой бытовой задачей (квартира, переезд, мебель, дача);\n"
    "hours — сколько часов реально длится работа, число или null;\n"
    "why — одно живое предложение ему на «ты» (до 110 символов): чем хороша шабашка или в чём подвох.\n\n"
    "Ответ — строго JSON-объект вида {\"items\": [ ... ]} без пояснений."
)

FIELDS = ("id", "is_offer", "scam", "scam_reason", "job", "pay_per_hour_rub", "effort", "idle_share",
          "trip", "place", "interesting", "needs", "why", "premium_place", "private", "hours")


class LLMUnavailable(Exception):
    pass


class LLM:
    def __init__(self, token: str | None, models: list[str], cap_left: int, timeout: int = 60):
        self.token = token
        self.models = [m for m in models if m]
        self.cap_left = cap_left
        self.timeout = timeout
        self.calls = 0
        self.disabled_reason: str | None = None if token else "нет токена"

    @property
    def available(self) -> bool:
        return bool(self.token) and self.disabled_reason is None and self.cap_left > 0 and bool(self.models)

    def _post(self, model: str, user_content: str) -> str:
        body = {
            "model": model,
            "temperature": 0.1,
            "max_tokens": 2500,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_content}],
        }
        req = urllib.request.Request(
            ENDPOINT, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    def assess(self, posts: list[tuple[str, str]], batch: int = 8) -> dict[str, dict]:
        """posts: [(id, text)] -> {id: признаки}. Ошибки не роняют бота — просто пусто."""
        out: dict[str, dict] = {}
        for i in range(0, len(posts), batch):
            if not self.available:
                break
            chunk = posts[i:i + batch]
            payload = "\n\n".join(f"### id={pid}\n{_trim(txt)}" for pid, txt in chunk)
            content = None
            for model in list(self.models):
                try:
                    self.calls += 1
                    self.cap_left -= 1
                    content = self._post(model, "Объявления:\n\n" + payload)
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        self.disabled_reason = "лимит запросов на сегодня"
                        break
                    if e.code in (401, 403):
                        self.disabled_reason = f"нет доступа к GitHub Models (HTTP {e.code})"
                        break
                    # 400/404 — модель недоступна, пробуем следующую
                    self.models.remove(model)
                    continue
                except Exception as e:  # noqa: BLE001 — тип ошибки в лог (без текста, там может быть что угодно)
                    self.disabled_reason = f"сеть/таймаут ({type(e).__name__})"
                    break
            if not content:
                continue
            for item in _parse(content):
                pid = str(item.get("id", "")).strip()
                if pid:
                    out[pid] = _clean(item)
        return out


def _redact(t: str) -> str:
    t = re.sub(r"(?:\+7|\b8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b", "[телефон]", t or "")
    t = re.sub(r"https?://\S+|t\.me/\S+|tg://\S+", "[ссылка]", t)
    return re.sub(r"(?<![\w@])@[A-Za-z0-9_]{3,32}", "[контакт]", t)


def _trim(t: str, n: int = 900) -> str:
    t = re.sub(r"\s+", " ", _redact(t)).strip()
    return t if len(t) <= n else t[:n] + " …"


def _parse(content: str) -> list[dict]:
    s = content.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, flags=re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    items = data.get("items") if isinstance(data, dict) else data
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


def _num(v, lo, hi):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def _clean(item: dict) -> dict:
    c = {k: item.get(k) for k in FIELDS}
    for b in ("is_offer", "scam", "trip", "premium_place", "private"):
        c[b] = c[b] if isinstance(c[b], bool) else None
    c["pay_per_hour_rub"] = _num(c["pay_per_hour_rub"], 50, 20000)
    c["effort"] = _num(c["effort"], 1, 5)
    c["idle_share"] = _num(c["idle_share"], 0, 1)
    c["interesting"] = _num(c["interesting"], 0, 1)
    c["hours"] = _num(c["hours"], 0.1, 72)
    c["needs"] = [str(x)[:20] for x in (c["needs"] or []) if isinstance(x, (str,))][:5] if isinstance(c["needs"], list) else []
    for s in ("job", "place", "why", "scam_reason"):
        c[s] = str(c[s])[:160] if isinstance(c[s], str) and c[s].strip() and c[s].strip().lower() != "null" else None
    return c
