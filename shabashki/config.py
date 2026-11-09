"""Настройки из переменных окружения (в GitHub — из Secrets). В коде секретов нет."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


class ConfigError(Exception):
    pass


@dataclass
class Config:
    api_id: int | None          # только для режима закрытых чатов (отдельный аккаунт)
    api_hash: str | None
    bot_token: str
    tg_password: str | None
    folder: str | None
    channels: list[str]
    home: tuple[float, float] | None
    state_path: str
    state_secrets: list[str]
    strong_key: bool
    owner_id: int | None
    llm_token: str | None
    llm_models: list[str]
    llm_daily_cap: int
    geocode: bool
    user_agent: str
    loop_seconds: int

    @classmethod
    def load(cls) -> "Config":
        if not _env("BOT_TOKEN"):
            raise ConfigError("не задан секрет BOT_TOKEN (токен твоего бота от @BotFather)")
        api_id = None
        if _env("TG_API_ID"):
            try:
                api_id = int(_env("TG_API_ID"))
            except ValueError:
                raise ConfigError("TG_API_ID должен быть числом (смотри my.telegram.org)") from None
        api_hash = _env("TG_API_HASH")
        home = None
        if _env("HOME_COORDS"):
            try:
                lat, lon = (float(x) for x in _env("HOME_COORDS").replace(" ", "").split(",")[:2])
                home = (lat, lon)
            except ValueError:
                home = None
        # Ключ шифрования состояния: секрет STATE_KEY (рекомендуется, а для закрытых чатов обязателен);
        # запасной — производный от токена бота (смена токена через /revoke меняет и ключ).
        bot_token = _env("BOT_TOKEN")
        secrets = [s for s in (_env("STATE_KEY"), f"{bot_token}:shabashki-state-v3") if s]
        owner = _env("OWNER_ID")
        repo = _env("GITHUB_REPOSITORY", "local")
        return cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            tg_password=_env("TG_PASSWORD"),
            folder=_env("FOLDER"),
            channels=[x for x in re.split(r"[\s,;]+", _env("CHANNELS", "") or "") if x],
            home=home,
            state_path=_env("STATE_PATH", "state.bin"),
            state_secrets=secrets,
            strong_key=bool(_env("STATE_KEY")),
            owner_id=int(owner) if owner and owner.isdigit() else None,
            llm_token=_env("LLM_TOKEN") or _env("GITHUB_TOKEN"),
            llm_models=(_env("LLM_MODELS", "openai/gpt-4.1-mini,openai/gpt-4o-mini") or "").split(","),
            llm_daily_cap=int(_env("LLM_DAILY_CAP", "120")),
            geocode=_env("GEOCODE", "1") != "0",
            user_agent=f"shabashki-bot/1.0 (+https://github.com/{repo})",
            loop_seconds=max(30, int(_env("LOOP_SECONDS", "60"))),
        )
