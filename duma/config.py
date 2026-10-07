"""Settings, read from the environment and an optional `.env`.

A missing or malformed variable stops the bot at startup, and the error names
the VARIABLE, never its value: these are credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from dotenv import dotenv_values

DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Same floor the API enforces on its side (`ASSISTANT_TOKEN_MIN_LENGTH`).
API_TOKEN_MIN_LENGTH = 32


class ConfigError(Exception):
    """Names the offending variable; never carries its value."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(repr=False)
    admin_chat_id: int
    allowed_user_ids: frozenset[int]
    api_url: str
    api_token: str = field(repr=False)
    anthropic_api_key: str = field(repr=False)
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    max_turns: int = 8
    max_tokens_per_message: int = 4000
    session_max_tokens: int = 100_000
    # US dollars of model spend per day, all admins together. 0 means no cap.
    daily_budget_usd: float = 5.0
    # How long a Guardar/Cancelar button stays valid.
    confirm_ttl_min: int = 15
    # A topic nobody has written in for this long is closed and its sessions deleted. 0 keeps them forever.
    session_idle_hours: int = 24
    # PostgreSQL for sessions and confirmations: BOT_DATABASE_URL, else the one Cloudron gives the app
    # (CLOUDRON_POSTGRESQL_URL). Neither: a SQLite file in `state_dir`.
    database_url: str = field(default="", repr=False)
    database_schema: str = "duma"
    # Whisper model for voice notes, where its files live (default: `state_dir`/whisper) and its limits.
    voice_model: str = "small"
    voice_dir: Optional[Path] = None
    voice_threads: int = 2
    voice_max_s: int = 120
    api_timeout_s: float = 30.0
    api_max_per_min: int = 60
    # A message older than this when Duma reads it (it was down, or restarting) is skipped, not answered.
    stale_after_s: int = 300
    state_dir: Path = Path("./state")
    tz: str = "America/Monterrey"


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def _int(env: Mapping[str, str], name: str, default: Optional[int] = None) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"{name} is not set")
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None


def load(env: Optional[Mapping[str, str]] = None, env_file: Optional[Path] = DEFAULT_ENV_FILE) -> Settings:
    """Settings from `env` (default: the real environment) over the `.env` file.

    The process environment wins over the file, so a value can be overridden
    for one run without editing it. Pass `env_file=None` to skip the file.
    """
    merged: dict[str, str] = {}
    if env_file is not None and Path(env_file).is_file():
        merged.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    merged.update(os.environ if env is None else env)

    ids_raw = _required(merged, "TELEGRAM_ALLOWED_USER_IDS")
    try:
        allowed = frozenset(int(part) for part in ids_raw.split(",") if part.strip())
    except ValueError:
        raise ConfigError("TELEGRAM_ALLOWED_USER_IDS must be integers separated by commas") from None
    if not allowed:
        raise ConfigError("TELEGRAM_ALLOWED_USER_IDS is empty")

    api_token = _required(merged, "BOT_API_TOKEN")
    if len(api_token) < API_TOKEN_MIN_LENGTH:
        raise ConfigError(f"BOT_API_TOKEN must have at least {API_TOKEN_MIN_LENGTH} characters")

    return Settings(
        telegram_bot_token=_required(merged, "TELEGRAM_BOT_TOKEN"),
        admin_chat_id=_int(merged, "TELEGRAM_ADMIN_CHAT_ID"),
        allowed_user_ids=allowed,
        api_url=_required(merged, "BOT_API_URL").rstrip("/"),
        api_token=api_token,
        anthropic_api_key=_required(merged, "ANTHROPIC_API_KEY"),
        model=(merged.get("BOT_MODEL") or "claude-sonnet-5-5").strip(),
        effort=(merged.get("BOT_EFFORT") or "medium").strip(),
        max_turns=_int(merged, "BOT_MAX_TURNS", 8),
        max_tokens_per_message=_int(merged, "BOT_MAX_TOKENS_PER_MESSAGE", 4000),
        session_max_tokens=_int(merged, "BOT_SESSION_MAX_TOKENS", 100_000),
        daily_budget_usd=_float(merged, "BOT_DAILY_BUDGET_USD", 5.0),
        confirm_ttl_min=_int(merged, "BOT_CONFIRM_TTL_MIN", 15),
        session_idle_hours=_int(merged, "BOT_SESSION_IDLE_HOURS", 24),
        database_url=(merged.get("BOT_DATABASE_URL") or merged.get("CLOUDRON_POSTGRESQL_URL") or "").strip(),
        database_schema=(merged.get("BOT_DATABASE_SCHEMA") or "duma").strip(),
        voice_model=(merged.get("BOT_VOICE_MODEL") or "small").strip(),
        voice_dir=Path(merged["BOT_VOICE_DIR"].strip()) if (merged.get("BOT_VOICE_DIR") or "").strip() else None,
        voice_threads=_int(merged, "BOT_VOICE_THREADS", 2),
        voice_max_s=_int(merged, "BOT_VOICE_MAX_S", 120),
        api_timeout_s=_float(merged, "BOT_API_TIMEOUT_S", 30.0),
        api_max_per_min=_int(merged, "BOT_API_MAX_PER_MIN", 60),
        stale_after_s=_int(merged, "BOT_STALE_AFTER_S", 300),
        state_dir=Path((merged.get("BOT_STATE_DIR") or "./state").strip()),
        tz=(merged.get("BOT_TZ") or "America/Monterrey").strip(),
    )
