"""Central configuration, loaded once from the environment.

Everything the bot needs to boot lives here so there is exactly one place that
reads os.environ. Fail fast and loudly on missing required values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required environment configuration is missing."""


def _get(name: str, *, required: bool = True, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if required and (value is None or value.strip() == ""):
        raise ConfigError(
            f"Missing required environment variable: {name}\n"
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, kw_only=True)
class Settings:
    """Immutable runtime settings."""

    # --- Discord -----------------------------------------------------------
    discord_token: str
    command_prefix: str = "!"

    # --- Supabase ----------------------------------------------------------
    supabase_url: str
    supabase_service_key: str

    # --- Groq --------------------------------------------------------------
    groq_api_key: str | None = None
    groq_default_model: str = "llama-3.3-70b-versatile"

    # --- Behaviour ---------------------------------------------------------
    log_level: str = "INFO"
    log_file: str = "logs/candy.log"

    # --- Feature flags -----------------------------------------------------
    enable_realtime: bool = True
    dev_guild_ids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def has_groq(self) -> bool:
        return bool(self.groq_api_key)


def load_settings() -> Settings:
    """Read and validate environment configuration."""
    raw_dev = os.getenv("DEV_GUILD_IDS", "")
    dev_guild_ids: list[int] = []
    for chunk in raw_dev.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            dev_guild_ids.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"DEV_GUILD_IDS contains a non-numeric id: {chunk!r}") from exc

    return Settings(
        discord_token=_get("DISCORD_TOKEN") or "",
        command_prefix=os.getenv("COMMAND_PREFIX", "!"),
        supabase_url=_get("SUPABASE_URL") or "",
        supabase_service_key=_get("SUPABASE_SERVICE_KEY") or "",
        # Groq is optional so the bot still boots without it.
        groq_api_key=os.getenv("GROQ_API_KEY") or None,
        groq_default_model=os.getenv("GROQ_DEFAULT_MODEL", "llama-3.3-70b-versatile"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file=os.getenv("LOG_FILE", "logs/candy.log"),
        enable_realtime=_get_bool("ENABLE_REALTIME", True),
        dev_guild_ids=tuple(dev_guild_ids),
    )


try:
    settings = load_settings()
except ConfigError as exc:  # pragma: no cover - import-time guard
    raise SystemExit(f"[config] {exc}") from exc

__all__ = ["Settings", "ConfigError", "settings", "load_settings"]
