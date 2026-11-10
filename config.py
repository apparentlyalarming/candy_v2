"""Central configuration, loaded once from the environment.

Everything the bot needs to boot lives here so there is exactly one place that
reads os.environ. Fail fast and loudly on missing required values.
"""

from __future__ import annotations

import logging
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
    groq_default_model: str = "openai/gpt-oss-120b"

    # --- Behaviour ---------------------------------------------------------
    log_level: str = "INFO"
    log_file: str = "logs/candy.log"

    # --- Feature flags -----------------------------------------------------
    enable_realtime: bool = True
    dev_guild_ids: tuple[int, ...] = field(default_factory=tuple)

    # --- Admin bridge (owner admin dashboard) ------------------------------
    # Publishes guild size snapshots, drains the DM outbox, and runs queued
    # admin requests. Turn off to run the bot with no admin wiring at all.
    admin_bridge_enabled: bool = True
    # Separate from the bridge because it is the privacy-sensitive half: when
    # true the bot stores every DM it receives (see the Privacy Policy).
    capture_dms: bool = True
    # Optional Discord user ID to receive an embed copy of every incoming DM.
    # This is read from the environment so forwarding needs no DB lookup.
    dm_forward_user_id: int | None = None

    # --- Host rotation reminder --------------------------------------------
    # bot-hosting.net's free tier requires a manual renewal every 4 days or the
    # bot is stopped. This is measured in DAYS SINCE THE LAST RESTART (see
    # services/rotation_reminder.py for why uptime is the only clock the bot can
    # observe). Set to 0 to disable the reminder entirely.
    rotation_reminder_days: float = 3.0

    # --- Activity tracking + monthly reports -------------------------------
    # Counters live in memory and are flushed in one RPC per guild. Turning
    # this off stops the collection entirely (the report then has no numbers,
    # but existing rows are untouched).
    activity_tracking_enabled: bool = True
    activity_flush_seconds: int = 300
    # Per-member/channel/hour rows older than this are pruned. Daily totals,
    # reports, boosts and moderation events are kept.
    report_retention_days: int = 400
    # Audit-log poll cadence and how far back the first poll reaches. Discord
    # audit entries expire after 45 days, so the backfill is capped below that.
    moderation_poll_seconds: int = 600
    moderation_backfill_days: int = 30

    @property
    def has_groq(self) -> bool:
        return bool(self.groq_api_key)


def _get_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Read an optional float, falling back to ``default`` on anything odd.

    A bad value here must not stop the bot from booting: the reminder is a
    convenience, not a core function. Invalid input is logged and ignored.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "%s is not a number (%r); using default %s", name, raw, default
        )
        return default
    if value < minimum:
        logging.getLogger(__name__).warning(
            "%s must be >= %s (got %s); using default %s", name, minimum, value, default
        )
        return default
    return value


def _get_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    """Read an optional integer, falling back to ``default`` on bad input.

    A typo in an operational knob must not stop the bot from booting; the
    value is clamped into range and a warning is logged instead.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "%s is not an integer (%r); using default %s", name, raw, default
        )
        return default
    if value < minimum:
        logging.getLogger(__name__).warning(
            "%s must be >= %s (got %s); using default %s", name, minimum, value, default
        )
        return default
    if maximum is not None and value > maximum:
        logging.getLogger(__name__).warning(
            "%s must be <= %s (got %s); using default %s", name, maximum, value, default
        )
        return default
    return value


def _get_optional_discord_id(name: str) -> int | None:
    """Read an optional Discord snowflake from the environment."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    if not raw.isdecimal():
        raise ConfigError(f"{name} must be a numeric Discord user ID.")
    return int(raw)


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
        groq_default_model=os.getenv("GROQ_DEFAULT_MODEL", "openai/gpt-oss-120b"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file=os.getenv("LOG_FILE", "logs/candy.log"),
        enable_realtime=_get_bool("ENABLE_REALTIME", True),
        admin_bridge_enabled=_get_bool("ADMIN_BRIDGE_ENABLED", True),
        capture_dms=_get_bool("CAPTURE_DMS", True),
        dm_forward_user_id=_get_optional_discord_id("DM_FORWARD_USER_ID"),
        rotation_reminder_days=_get_float("ROTATION_REMINDER_DAYS", 3.0),
        activity_tracking_enabled=_get_bool("ACTIVITY_TRACKING_ENABLED", True),
        activity_flush_seconds=_get_int(
            "ACTIVITY_FLUSH_SECONDS", 300, minimum=60, maximum=3600
        ),
        report_retention_days=_get_int(
            "REPORT_RETENTION_DAYS", 400, minimum=30, maximum=3650
        ),
        moderation_poll_seconds=_get_int(
            "MODERATION_POLL_SECONDS", 600, minimum=120, maximum=3600
        ),
        moderation_backfill_days=_get_int(
            "MODERATION_BACKFILL_DAYS", 30, minimum=0, maximum=45
        ),
        dev_guild_ids=tuple(dev_guild_ids),
    )


try:
    settings = load_settings()
except ConfigError as exc:  # pragma: no cover - import-time guard
    raise SystemExit(f"[config] {exc}") from exc

__all__ = ["Settings", "ConfigError", "settings", "load_settings"]
