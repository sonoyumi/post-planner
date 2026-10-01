"""Settings from .env in the current folder. Every mistake is reported at once."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

AI_MODES = ("off", "claude")
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


class ConfigError(Exception):
    """Wrong or missing settings; the message lists every problem."""


@dataclass(frozen=True)
class Settings:
    secret_key: str = field(repr=False)
    bot_token: str = field(default="", repr=False)
    timezone: str = "Europe/Rome"
    host: str = "127.0.0.1"
    port: int = 8095
    cookie_secure: bool = False
    data_dir: Path = Path("data")
    team_name: str = "Post planner"
    ai_mode: str = "off"
    anthropic_api_key: str | None = field(default=None, repr=False)
    claude_model: str = "claude-opus-5-5"
    ai_daily_limit: int = 100

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "post-planner.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


def _int(name: str, default: int, errors: list[str], low: int = 0, high: int = 10**9) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name}: нужно целое число, получено {raw!r}")
        return default
    if not low <= value <= high:
        errors.append(f"{name}: допустимо от {low} до {high}, получено {value}")
        return default
    return value


def load_settings(env_file: Path | None = Path(".env"), need_bot: bool = True) -> Settings:
    if env_file is not None:
        load_dotenv(env_file)
    errors: list[str] = []
    secret = os.getenv("SECRET_KEY", "").strip()
    if len(secret) < 32:
        errors.append("SECRET_KEY: случайная строка не короче 32 символов "
                      "(python3 -c \"import secrets; print(secrets.token_hex(32))\")")
    token = os.getenv("BOT_TOKEN", "").strip()
    if need_bot and not TOKEN_RE.match(token):
        errors.append("BOT_TOKEN: токен бота от @BotFather вида 123456789:AA… (бот — администратор каналов)")
    tz = os.getenv("TIMEZONE", "").strip() or "Europe/Rome"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"TIMEZONE: неизвестный часовой пояс {tz!r} (пример: Europe/Rome)")
        tz = "Europe/Rome"
    ai_mode = (os.getenv("AI_MODE", "").strip() or "off").lower()
    key = os.getenv("ANTHROPIC_API_KEY", "").strip() or None
    if ai_mode not in AI_MODES:
        errors.append(f"AI_MODE: одно из {', '.join(AI_MODES)}, получено {ai_mode!r}")
    elif ai_mode == "claude" and not key:
        errors.append("AI_MODE=claude: нужен ANTHROPIC_API_KEY (или AI_MODE=off)")
    settings = Settings(
        secret_key=secret,
        bot_token=token,
        timezone=tz,
        host=os.getenv("HOST", "").strip() or "127.0.0.1",
        port=_int("PORT", 8095, errors, 1, 65535),
        cookie_secure=os.getenv("COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"},
        data_dir=Path(os.getenv("DATA_DIR", "").strip() or "data"),
        team_name=os.getenv("TEAM_NAME", "").strip() or "Post planner",
        ai_mode=ai_mode,
        anthropic_api_key=key,
        claude_model=os.getenv("CLAUDE_MODEL", "").strip() or "claude-opus-5-5",
        ai_daily_limit=_int("AI_DAILY_LIMIT", 100, errors),
    )
    if errors:
        raise ConfigError("Ошибки в .env:\n  - " + "\n  - ".join(errors))
    return settings
