"""In-memory cache of per-guild settings.

Command handlers read from here, never from the network on a per-command basis.
The cache is filled at startup and kept fresh by the realtime listener, which
re-fetches whole rows on any change signal.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

from utils.discord_ids import to_id
from utils.logging_setup import get_logger

log = get_logger("db.cache")


# --- defaults ---------------------------------------------------------------
# Must mirror the column defaults in supabase/schema.sql. Used when a guild has not been
# written to the DB yet (bot invited, no settings row created).

DEFAULT_SETTINGS: dict[str, Any] = {
    "prefix": "!",
    "prefix_enabled": True,
    "log_channel_id": None,
    "log_enabled": True,
    "log_categories": ["config", "errors", "qotd"],
    # chat AI
    "ai_enabled": False,
    "ai_model": "llama-3.3-70b-versatile",
    "ai_system_prompt": (
        "You are Candy, a friendly and helpful Discord bot. "
        "Keep replies short, warm and casual."
    ),
    "ai_max_tokens": 512,
    "ai_temperature": 0.70,
    "ai_context_messages": 10,
    "ai_memory_enabled": True,
    "ai_memory_days": 30,
    "ai_memory_ignored_channels": [],
    "ai_user_cooldown_seconds": 5,
    "ai_all_channels": False,
    "ai_channel_ids": [],
    # question of the day
    "qotd_enabled": False,
    "qotd_channel_id": None,
    "qotd_role_id": None,
    "qotd_time": None,
    "qotd_timezone": "UTC",
    "qotd_days": [],
    "qotd_empty_queue_notice": False,
    "qotd_last_fired_at": None,
}


@dataclass
class GuildConfig:
    """Read-only view of one guild's settings.

    Wraps the raw row so callers get typed accessors and never have to
    remember that ``ai_channel_ids`` may arrive as a list of ints or strings.
    """

    guild_id: int
    raw: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.raw and self.raw[key] is not None:
            return self.raw[key]
        return DEFAULT_SETTINGS.get(key, default)

    # --- typed accessors ---------------------------------------------------

    @property
    def prefix(self) -> str:
        return str(self.get("prefix") or "!")

    @property
    def prefix_enabled(self) -> bool:
        """Whether text commands like !hug are active for this guild."""
        return bool(self.get("prefix_enabled"))

    # logging
    @property
    def log_enabled(self) -> bool:
        return bool(self.get("log_enabled"))

    @property
    def log_categories(self) -> list[str]:
        raw = self.get("log_categories") or []
        return [str(c) for c in raw if isinstance(c, str)]

    @property
    def log_channel_id(self) -> int | None:
        return to_id(self.raw.get("log_channel_id"))

    # chat AI
    @property
    def ai_enabled(self) -> bool:
        return bool(self.get("ai_enabled"))

    @property
    def ai_model(self) -> str:
        return str(self.get("ai_model"))

    @property
    def ai_system_prompt(self) -> str:
        return str(self.get("ai_system_prompt"))

    @property
    def ai_max_tokens(self) -> int:
        return int(self.get("ai_max_tokens"))

    @property
    def ai_temperature(self) -> float:
        return float(self.get("ai_temperature"))

    @property
    def ai_context_messages(self) -> int:
        return int(self.get("ai_context_messages"))

    @property
    def ai_memory_enabled(self) -> bool:
        return bool(self.get("ai_memory_enabled"))

    @property
    def ai_memory_days(self) -> int:
        """Retention window in days. 0 means keep forever."""
        return int(self.get("ai_memory_days"))

    @property
    def ai_memory_ignored_channels(self) -> list[int]:
        raw = self.get("ai_memory_ignored_channels") or []
        result: list[int] = []
        for item in raw:
            parsed = to_id(item)
            if parsed is not None:
                result.append(parsed)
        return result

    def ai_memory_ignores(self, channel_id: int) -> bool:
        """True when this channel is opted out of memory (no store, no recall)."""
        return channel_id in self.ai_memory_ignored_channels

    @property
    def ai_context_effective(self) -> int:
        """How many past messages to actually feed into a request.

        Memory off short-circuits the CONTEXT depth as well as the writes: a
        memory that is not being saved must not be read either, or turning it off
        would still leak stale context into replies. The stored
        ``ai_context_messages`` is preserved untouched so re-enabling restores
        the previous depth rather than resetting it to 0.
        """
        if not self.ai_memory_enabled:
            return 0
        return max(0, self.ai_context_messages)

    @property
    def ai_user_cooldown_seconds(self) -> int:
        return int(self.get("ai_user_cooldown_seconds"))

    @property
    def ai_all_channels(self) -> bool:
        return bool(self.get("ai_all_channels"))

    @property
    def ai_channel_ids(self) -> list[int]:
        raw = self.get("ai_channel_ids") or []
        result: list[int] = []
        for item in raw:
            parsed = to_id(item)
            if parsed is not None:
                result.append(parsed)
        return result

    def ai_allowed_in(self, channel_id: int) -> bool:
        """True when the AI should respond in this channel."""
        if not self.ai_enabled:
            return False
        if self.ai_all_channels:
            return True
        allowed = self.ai_channel_ids
        return bool(allowed) and channel_id in allowed

    # question of the day
    @property
    def qotd_enabled(self) -> bool:
        return bool(self.get("qotd_enabled"))

    @property
    def qotd_channel_id(self) -> int | None:
        return to_id(self.raw.get("qotd_channel_id"))

    @property
    def qotd_role_id(self) -> int | None:
        return to_id(self.raw.get("qotd_role_id"))

    @property
    def qotd_time(self) -> str | None:
        value = self.get("qotd_time")
        return None if value is None else str(value)

    @property
    def qotd_timezone(self) -> str:
        return str(self.get("qotd_timezone") or "UTC")

    @property
    def qotd_days(self) -> list[int]:
        raw = self.get("qotd_days") or []
        return [int(d) for d in raw if isinstance(d, (int, str)) and str(d).isdigit()]

    @property
    def qotd_empty_queue_notice(self) -> bool:
        return bool(self.get("qotd_empty_queue_notice"))

    @property
    def qotd_last_fired_at(self) -> str | None:
        value = self.raw.get("qotd_last_fired_at")
        return None if value is None else str(value)

    def as_dict(self) -> dict[str, Any]:
        """Merged view: DB row on top of defaults."""
        merged = copy.deepcopy(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in self.raw.items() if v is not None})
        merged["guild_id"] = self.guild_id
        return merged


# --- the cache --------------------------------------------------------------

_lock = threading.RLock()
_cache: dict[int, GuildConfig] = {}


def load_all(rows: Iterable[dict[str, Any]]) -> int:
    """Replace the entire cache from a full settings SELECT."""
    with _lock:
        _cache.clear()
        count = 0
        for row in rows:
            guild_id = to_id(row.get("guild_id"))
            if guild_id is None:
                continue
            _cache[guild_id] = GuildConfig(guild_id=guild_id, raw=dict(row))
            count += 1
    log.info("Loaded %d guild settings rows into cache", count)
    return count


def put(row: dict[str, Any]) -> None:
    """Insert or replace one guild's settings (called by the realtime listener)."""
    guild_id = to_id(row.get("guild_id"))
    if guild_id is None:
        log.warning("Ignoring settings row with unusable guild_id: %r", row.get("guild_id"))
        return
    with _lock:
        _cache[guild_id] = GuildConfig(guild_id=guild_id, raw=dict(row))
    log.debug("Cache updated for guild %s", guild_id)


def patch(guild_id: int, changes: dict[str, Any]) -> None:
    """Merge partial changes into a cached entry (used for optimistic local writes)."""
    gid = to_id(guild_id)
    if gid is None:
        return
    with _lock:
        existing = _cache.get(gid)
        raw = dict(existing.raw) if existing else {}
        raw.update(changes)
        _cache[gid] = GuildConfig(guild_id=gid, raw=raw)


def drop(guild_id: int) -> None:
    with _lock:
        _cache.pop(to_id(guild_id) or -1, None)


def get(guild_id: int) -> GuildConfig:
    """Get a guild's config, synthesising defaults when absent."""
    gid = to_id(guild_id)
    if gid is None:
        raise ValueError(f"invalid guild id: {guild_id!r}")
    with _lock:
        existing = _cache.get(gid)
    if existing is not None:
        return existing
    # Not in the DB yet: return defaults without caching, so the next realtime
    # event still populates the real row.
    return GuildConfig(guild_id=gid, raw={})


def has(guild_id: int) -> bool:
    return (to_id(guild_id) or -1) in _cache


def all_guild_ids() -> list[int]:
    with _lock:
        return list(_cache.keys())


def size() -> int:
    with _lock:
        return len(_cache)


__all__ = [
    "DEFAULT_SETTINGS",
    "GuildConfig",
    "load_all",
    "put",
    "patch",
    "drop",
    "get",
    "has",
    "all_guild_ids",
    "size",
]
