"""ConfigService: THE single write path for bot configuration.

Every mutation - from a slash command, from a realtime event, or from a future
web-triggered flow - goes through here. Validation lives here and nowhere else,
so the bot and the dashboard cannot drift on what counts as valid.

The web app implements the same rules (see candy_web buildplan Phase 5); this
module is the reference implementation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from db import config_cache, supabase_client
from db.config_cache import DEFAULT_SETTINGS, GuildConfig
from services.render import TemplateError, validate_template_list
from utils.discord_ids import to_id, to_id_list
from utils.logging_setup import get_logger

log = get_logger("services.config")

# --- limits (mirror schema.sql CHECK constraints) ---------------------------

MAX_PROMPT_LENGTH = 4000
MAX_PREFIX_LENGTH = 5
MAX_QUESTION_LENGTH = 500
AI_TEMPERATURE_MIN = 0.0
AI_TEMPERATURE_MAX = 2.0
AI_MAX_TOKENS_MIN = 1
AI_MAX_TOKENS_MAX = 8192
AI_CONTEXT_MIN = 0
AI_CONTEXT_MAX = 50
AI_MEMORY_DAYS_MIN = 0
AI_MEMORY_DAYS_MAX = 3650
COOLDOWN_MIN = 0
COOLDOWN_MAX = 3600

# Per-turn text cap. Stops one huge paste from bloating a stored exchange; the
# window is a memory, not an archive.
MEMORY_TURN_MAX_CHARS = 2000
QOTD_DAYS_VALID = {1, 2, 3, 4, 5, 6, 7}


class ConfigError(ValueError):
    """Raised when a proposed configuration value is invalid."""


class ConfigService:
    """Reads and writes guild configuration.

    Writes go to Supabase, then update the local cache optimistically so the
    change is visible immediately on this process. The realtime listener will
    confirm shortly after (and is the mechanism that updates *other* processes).
    """

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def get(self, guild_id: int | Any) -> GuildConfig:
        return config_cache.get(to_id(guild_id) or 0)

    def ensure_guild_row(self, guild_id: int, *, name: str | None = None, icon_url: str | None = None) -> None:
        """Make sure a guilds row exists. Called on join and on startup sync."""
        gid = to_id(guild_id)
        if gid is None:
            raise ConfigError("invalid guild id")

        existing = supabase_client.select_one("guilds", filters={"id": gid})
        payload: dict[str, Any] = {"id": gid, "bot_present": True}
        if name:
            payload["name"] = name
        if icon_url is not None:
            payload["icon_url"] = icon_url

        if existing is None:
            supabase_client.upsert("guilds", payload, on_conflict="id")
            log.info("Registered guild %s (%s)", gid, name or "unknown")
        elif name and existing.get("name") != name:
            supabase_client.update("guilds", {"name": name, "icon_url": icon_url, "bot_present": True}, filters={"id": gid})

        self._ensure_settings_row(gid)

    def _ensure_settings_row(self, guild_id: int) -> None:
        if config_cache.has(guild_id):
            return
        existing = supabase_client.select_one("guild_settings", filters={"guild_id": guild_id})
        if existing is not None:
            config_cache.put(existing)
            return
        row = supabase_client.upsert("guild_settings", {"guild_id": guild_id}, on_conflict="guild_id")
        if row:
            config_cache.put(row[0])

    def mark_bot_absent(self, guild_id: int) -> None:
        gid = to_id(guild_id)
        if gid is None:
            return
        supabase_client.update("guilds", {"bot_present": False}, filters={"id": gid})
        log.info("Marked guild %s as bot_present=false", gid)

    # ------------------------------------------------------------------
    # generic write
    # ------------------------------------------------------------------

    def _update_settings(self, guild_id: int, changes: dict[str, Any]) -> None:
        """Write validated changes to Supabase and refresh the local cache."""
        gid = to_id(guild_id)
        if gid is None:
            raise ConfigError("invalid guild id")
        if not changes:
            return

        self._ensure_settings_row(gid)

        try:
            rows = supabase_client.update("guild_settings", changes, filters={"guild_id": gid})
        except Exception as exc:
            log.error("Failed to write settings for guild %s: %s", gid, exc, exc_info=True)
            raise ConfigError(f"database write failed: {exc}") from exc

        # Optimistic local update; realtime will confirm.
        config_cache.patch(gid, changes)
        log.info(
            "Config updated for guild %s by %s: %s",
            gid,
            _source(),
            ", ".join(sorted(changes.keys())),
        )
        if rows:
            config_cache.put(rows[0])

    # ------------------------------------------------------------------
    # general
    # ------------------------------------------------------------------

    def set_prefix(self, guild_id: int, prefix: str) -> None:
        cleaned = (prefix or "").strip()
        if not cleaned:
            raise ConfigError("prefix must not be empty")
        if len(cleaned) > MAX_PREFIX_LENGTH:
            raise ConfigError(f"prefix must be at most {MAX_PREFIX_LENGTH} characters")
        if " " in cleaned:
            raise ConfigError("prefix must not contain spaces")
        self._update_settings(guild_id, {"prefix": cleaned})

    def set_log_channel(self, guild_id: int, channel_id: int | None) -> None:
        self._update_settings(guild_id, {"log_channel_id": to_id(channel_id)})

    def set_prefix_enabled(self, guild_id: int, enabled: bool) -> None:
        self._update_settings(guild_id, {"prefix_enabled": bool(enabled)})

    # ------------------------------------------------------------------
    # logging
    # ------------------------------------------------------------------

    def set_log_enabled(self, guild_id: int, enabled: bool) -> None:
        self._update_settings(guild_id, {"log_enabled": bool(enabled)})

    def set_log_categories(self, guild_id: int, categories: Iterable[str]) -> None:
        """Replace the set of logged event categories."""
        from utils.event_log import VALID_CATEGORIES

        resolved = sorted({str(c).strip().lower() for c in (categories or []) if str(c).strip()})
        invalid = [c for c in resolved if c not in VALID_CATEGORIES]
        if invalid:
            raise ConfigError(
                f"unknown log categories: {', '.join(invalid)} "
                f"(valid: {', '.join(sorted(VALID_CATEGORIES))})"
            )
        self._update_settings(guild_id, {"log_categories": resolved})

    # ------------------------------------------------------------------
    # AI memory
    # ------------------------------------------------------------------

    def record_ai_interaction(
        self,
        *,
        guild_id: int,
        channel_id: int | None,
        user_id: int | None,
        user_display_name: str | None,
        result: Any,
    ) -> None:
        """Persist the SUMMARY of one AI exchange (never the full reply).

        Called after a successful reply. Failures are logged and swallowed: the
        user already has their answer, so a bookkeeping error must not surface.

        This is the ANALYTICS record (counts/tokens/latency and a one-line gist).
        The restart-safe conversation store is :meth:`record_memory_turn`.
        """
        gid = to_id(guild_id)
        if gid is None:
            return

        row = result.as_row() if hasattr(result, "as_row") else {}
        if not row:
            return

        row.update(
            {
                "guild_id": gid,
                "channel_id": to_id(channel_id),
                "discord_user_id": to_id(user_id),
                "user_display_name": (user_display_name or None),
                # user_id (the auth.users uuid) is intentionally left NULL: the
                # bot knows the Discord id, not the Supabase account. The RLS
                # "see your own" policy simply will not match bot-written rows,
                # which is the correct default until the web links the two.
            }
        )

        try:
            supabase_client.insert("ai_interactions", row)
        except Exception as exc:  # noqa: BLE001 - memory is best-effort
            log.debug("Could not record ai_interaction: %s", exc)

    def trim_ai_channel(self, guild_id: int, channel_id: int, keep: int = 50) -> int:
        """Keep only the newest ``keep`` summaries for a channel. Returns deleted count.

        LEGACY (migration_002): superseded by :meth:`prune_ai_memory`, which ages
        out the real conversation store by the guild's ``ai_memory_days`` window.
        Retained because ``ai_trim_channel()`` still exists in the database and the
        dashboard contract references it; nothing in the bot calls it any more.
        """
        gid = to_id(guild_id)
        cid = to_id(channel_id)
        if gid is None or cid is None:
            return 0
        try:
            return int(
                supabase_client.call_rpc(
                    "ai_trim_channel",
                    {"p_guild_id": gid, "p_channel_id": cid, "p_keep": max(1, int(keep))},
                )
                or 0
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("ai_trim_channel failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # chat AI
    # ------------------------------------------------------------------

    def set_ai_enabled(self, guild_id: int, enabled: bool) -> None:
        self._update_settings(guild_id, {"ai_enabled": bool(enabled)})

    def set_ai_model(self, guild_id: int, model: str) -> None:
        cleaned = (model or "").strip()
        if not cleaned:
            raise ConfigError("model must not be empty")
        if len(cleaned) > 100:
            raise ConfigError("model name is too long")
        self._update_settings(guild_id, {"ai_model": cleaned})

    def set_ai_system_prompt(self, guild_id: int, prompt: str) -> None:
        cleaned = (prompt or "").strip()
        if not cleaned:
            raise ConfigError("system prompt must not be empty")
        if len(cleaned) > MAX_PROMPT_LENGTH:
            raise ConfigError(f"system prompt must be at most {MAX_PROMPT_LENGTH} characters")
        self._update_settings(guild_id, {"ai_system_prompt": cleaned})

    def set_ai_max_tokens(self, guild_id: int, value: int) -> None:
        _check_range("max tokens", value, AI_MAX_TOKENS_MIN, AI_MAX_TOKENS_MAX)
        self._update_settings(guild_id, {"ai_max_tokens": int(value)})

    def set_ai_temperature(self, guild_id: int, value: float) -> None:
        _check_range("temperature", float(value), AI_TEMPERATURE_MIN, AI_TEMPERATURE_MAX)
        self._update_settings(guild_id, {"ai_temperature": round(float(value), 2)})

    def set_ai_context_messages(self, guild_id: int, value: int) -> None:
        _check_range("context messages", value, AI_CONTEXT_MIN, AI_CONTEXT_MAX)
        self._update_settings(guild_id, {"ai_context_messages": int(value)})

    def set_ai_memory_enabled(self, guild_id: int, enabled: bool) -> None:
        """Turn per-server memory on or off.

        Off means the AI neither reads past turns nor writes new ones. Existing
        ai_memory rows are deliberately NOT deleted, so turning it back on
        restores prior context. Read depth is preserved separately.
        """
        self._update_settings(guild_id, {"ai_memory_enabled": bool(enabled)})

    def set_ai_memory_days(self, guild_id: int, days: int) -> None:
        """Set the per-row retention window in days. 0 = keep forever.

        Per-row expiry: each stored turn dies on its own Nth birthday. There is
        no whole-table wipe at any point.
        """
        _check_range("memory days", days, AI_MEMORY_DAYS_MIN, AI_MEMORY_DAYS_MAX)
        self._update_settings(guild_id, {"ai_memory_days": int(days)})

    def set_ai_memory_ignored_channels(
        self, guild_id: int, channel_ids: Iterable[Any] | None
    ) -> None:
        """Replace the set of channels excluded from memory."""
        resolved = to_id_list(channel_ids or [])
        self._update_settings(guild_id, {"ai_memory_ignored_channels": resolved})

    def add_ai_memory_ignored_channel(self, guild_id: int, channel_id: Any) -> None:
        current = set(self.get(guild_id).ai_memory_ignored_channels)
        target = to_id(channel_id)
        if target is None:
            raise ConfigError("invalid channel id")
        current.add(target)
        self.set_ai_memory_ignored_channels(guild_id, current)

    def remove_ai_memory_ignored_channel(self, guild_id: int, channel_id: Any) -> None:
        current = set(self.get(guild_id).ai_memory_ignored_channels)
        target = to_id(channel_id)
        current.discard(target)
        self.set_ai_memory_ignored_channels(guild_id, current)

    # --- memory turns (the restart-safe store) -------------------------

    def record_memory_turn(
        self,
        *,
        guild_id: int,
        channel_id: int | None,
        user_id: int | None,
        user_display_name: str | None,
        user_content: str,
        assistant_content: str,
    ) -> None:
        """Persist one real exchange (prompt + reply) for restart-safe memory.

        Both halves are required: a reply with no prompt cannot be replayed into
        a conversation. Best-effort - the user already has their answer, so a
        bookkeeping failure must not surface.
        """
        gid = to_id(guild_id)
        if gid is None:
            return

        prompt = (user_content or "").strip()
        reply = (assistant_content or "").strip()
        if not prompt or not reply:
            return

        row: dict[str, Any] = {
            "guild_id": gid,
            "channel_id": to_id(channel_id),
            "discord_user_id": to_id(user_id),
            "user_display_name": (user_display_name or None),
            "user_content": prompt[:MEMORY_TURN_MAX_CHARS],
            "assistant_content": reply[:MEMORY_TURN_MAX_CHARS],
            # user_id (the auth.users uuid) is left NULL on purpose: the bot knows
            # the Discord id, not the linked Supabase account.
        }

        try:
            supabase_client.insert("ai_memory", row)
        except Exception as exc:  # noqa: BLE001 - memory is best-effort
            log.debug("Could not record ai_memory turn: %s", exc)

    def load_memory(
        self, guild_id: int, *, limit: int, discord_user_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Newest ``limit`` memory turns for a guild, oldest-first for replay.

        Filtered on guild_id only: memory is global per server, not per channel.
        When ``discord_user_id`` is given, rows are restricted to that author
        (the personalized path). Returns [] on any failure - memory is optional.
        """
        gid = to_id(guild_id)
        if gid is None or limit <= 0:
            return []

        filters: dict[str, Any] = {"guild_id": gid}
        uid = to_id(discord_user_id)
        if uid is not None:
            filters["discord_user_id"] = uid

        try:
            rows = supabase_client.select(
                "ai_memory",
                "user_content,assistant_content,discord_user_id,created_at",
                filters=filters,
                order_by="created_at.desc",
                limit=int(limit),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("load_memory failed: %s", exc)
            return []

        # Query returned newest-first; the prompt wants chronological order.
        return list(reversed(rows))

    def prune_ai_memory(self, guild_id: int, keep_days: int) -> int:
        """Delete this guild's turns older than ``keep_days``. Returns count.

        Runs on the same write pass as the insert, exactly like the old
        ai_trim_channel helper, so retention needs no scheduled job. A no-op when
        ``keep_days`` <= 0 (keep forever).
        """
        gid = to_id(guild_id)
        if gid is None or keep_days <= 0:
            return 0
        try:
            return int(
                supabase_client.call_rpc(
                    "ai_memory_prune",
                    {"p_guild_id": gid, "p_keep_days": max(1, int(keep_days))},
                )
                or 0
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("ai_memory_prune failed: %s", exc)
            return 0

    def set_ai_cooldown(self, guild_id: int, seconds: int) -> None:
        _check_range("cooldown", seconds, COOLDOWN_MIN, COOLDOWN_MAX)
        self._update_settings(guild_id, {"ai_user_cooldown_seconds": int(seconds)})

    def set_ai_channels(self, guild_id: int, channel_ids: Iterable[Any] | None) -> None:
        """Replace the AI channel allow-list. Empty list = disabled everywhere."""
        resolved = to_id_list(channel_ids or [])
        self._update_settings(guild_id, {"ai_channel_ids": resolved})

    def add_ai_channel(self, guild_id: int, channel_id: Any) -> None:
        current = set(self.get(guild_id).ai_channel_ids)
        target = to_id(channel_id)
        if target is None:
            raise ConfigError("invalid channel id")
        current.add(target)
        self.set_ai_channels(guild_id, current)

    def remove_ai_channel(self, guild_id: int, channel_id: Any) -> None:
        current = set(self.get(guild_id).ai_channel_ids)
        target = to_id(channel_id)
        current.discard(target)
        self.set_ai_channels(guild_id, current)

    def set_ai_all_channels(self, guild_id: int, enabled: bool) -> None:
        self._update_settings(guild_id, {"ai_all_channels": bool(enabled)})

    # ------------------------------------------------------------------
    # replies / fun actions
    # ------------------------------------------------------------------

    def list_actions(self) -> list[dict[str, Any]]:
        """The shipped catalog, ordered for display."""
        return supabase_client.select("reply_actions", order_by="sort_order")

    def get_action(self, key: str) -> dict[str, Any] | None:
        return supabase_client.select_one("reply_actions", filters={"key": key})

    def resolve_templates(self, guild_id: int, action: str) -> list[str]:
        """Resolution order, identical to the web preview:

        1. guild_replies row exists AND enabled -> its templates
        2. no row, or empty templates          -> reply_actions.default_templates
        3. still nothing                        -> [] (caller suppresses the reply)
        """
        gid = to_id(guild_id)
        if gid is None:
            return []

        override = supabase_client.select_one(
            "guild_replies", filters={"guild_id": gid, "action": action}
        )
        if override and override.get("enabled"):
            templates = [t for t in (override.get("templates") or []) if isinstance(t, str) and t.strip()]
            if templates:
                return templates

        catalog = self.get_action(action)
        if catalog:
            defaults = [
                t for t in (catalog.get("default_templates") or [])
                if isinstance(t, str) and t.strip()
            ]
            if defaults:
                return defaults
        return []

    def set_reply_templates(self, guild_id: int, action: str, templates: Iterable[str]) -> None:
        gid = to_id(guild_id)
        if gid is None:
            raise ConfigError("invalid guild id")
        if self.get_action(action) is None:
            raise ConfigError(f"unknown action: {action}")

        try:
            cleaned = validate_template_list(list(templates))
        except TemplateError as exc:
            raise ConfigError(str(exc)) from exc

        if not cleaned:
            # Empty list means "fall back to defaults"; store an explicit empty
            # row so the override is recorded as cleared rather than absent.
            cleaned = []

        self._ensure_settings_row(gid)
        rows = supabase_client.upsert(
            "guild_replies",
            {"guild_id": gid, "action": action, "templates": cleaned},
            on_conflict="guild_id,action",
        )
        log.info("Reply templates updated for guild %s action %s (%d entries)", gid, action, len(cleaned))
        _ = rows

    def add_reply_template(self, guild_id: int, action: str, template: str) -> None:
        current = self.resolve_templates(guild_id, action)
        current.append(template)
        self.set_reply_templates(guild_id, action, current)

    def remove_reply_template(self, guild_id: int, action: str, index: int) -> str:
        """Remove by 1-based index. Returns the removed template."""
        current = self.resolve_templates(guild_id, action)
        if index < 1 or index > len(current):
            raise ConfigError(f"index must be between 1 and {len(current)}")
        removed = current.pop(index - 1)
        self.set_reply_templates(guild_id, action, current)
        return removed

    def set_reply_enabled(self, guild_id: int, action: str, enabled: bool) -> None:
        gid = to_id(guild_id)
        if gid is None:
            raise ConfigError("invalid guild id")
        if self.get_action(action) is None:
            raise ConfigError(f"unknown action: {action}")

        self._ensure_settings_row(gid)
        existing = supabase_client.select_one(
            "guild_replies", filters={"guild_id": gid, "action": action}
        )
        payload: dict[str, Any] = {
            "guild_id": gid,
            "action": action,
            "enabled": bool(enabled),
        }
        if existing is None:
            # Preserve the resolved defaults so toggling off/on round-trips.
            payload["templates"] = self.resolve_templates(gid, action)
        supabase_client.upsert("guild_replies", payload, on_conflict="guild_id,action")

    def reset_reply_templates(self, guild_id: int, action: str) -> int:
        """Delete the override so the shipped defaults apply. Returns rows removed."""
        gid = to_id(guild_id)
        if gid is None:
            raise ConfigError("invalid guild id")
        rows = supabase_client.delete(
            "guild_replies", filters={"guild_id": gid, "action": action}
        )
        log.info("Reset reply templates for guild %s action %s", gid, action)
        return len(rows)

    def reply_override_state(self, guild_id: int, action: str) -> str:
        """'custom' | 'disabled' | 'default' - for display in /config replies list."""
        gid = to_id(guild_id)
        if gid is None:
            return "default"
        row = supabase_client.select_one(
            "guild_replies", filters={"guild_id": gid, "action": action}
        )
        if row is None:
            return "default"
        if not row.get("enabled"):
            return "disabled"
        return "custom"

    # ------------------------------------------------------------------
    # question of the day
    # ------------------------------------------------------------------

    def set_qotd_enabled(self, guild_id: int, enabled: bool) -> None:
        self._update_settings(guild_id, {"qotd_enabled": bool(enabled)})

    def set_qotd_channel(self, guild_id: int, channel_id: Any | None) -> None:
        self._update_settings(guild_id, {"qotd_channel_id": to_id(channel_id)})

    def set_qotd_role(self, guild_id: int, role_id: Any | None) -> None:
        self._update_settings(guild_id, {"qotd_role_id": to_id(role_id)})

    def set_qotd_time(self, guild_id: int, hour: int, minute: int = 0) -> None:
        if not (0 <= hour <= 23):
            raise ConfigError("hour must be between 0 and 23")
        if not (0 <= minute <= 59):
            raise ConfigError("minute must be between 0 and 59")
        self._update_settings(guild_id, {"qotd_time": f"{hour:02d}:{minute:02d}:00"})

    def set_qotd_timezone(self, guild_id: int, timezone_name: str) -> None:
        cleaned = (timezone_name or "").strip()
        if not cleaned:
            raise ConfigError("timezone must not be empty")
        try:
            ZoneInfo(cleaned)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                f"unknown timezone {cleaned!r}. Use an IANA name such as Asia/Kolkata or Europe/London."
            ) from exc
        self._update_settings(guild_id, {"qotd_timezone": cleaned})

    def set_qotd_days(self, guild_id: int, days: Iterable[int] | None) -> None:
        resolved = sorted({int(d) for d in (days or [])})
        invalid = [d for d in resolved if d not in QOTD_DAYS_VALID]
        if invalid:
            raise ConfigError(f"invalid weekday numbers: {invalid} (use 1=Mon .. 7=Sun)")
        self._update_settings(guild_id, {"qotd_days": resolved})

    def set_qotd_empty_notice(self, guild_id: int, enabled: bool) -> None:
        self._update_settings(guild_id, {"qotd_empty_queue_notice": bool(enabled)})

    # --- queue ---------------------------------------------------------

    def qotd_queue(self, guild_id: int) -> list[dict[str, Any]]:
        """Queued questions in FIFO order (asked_at IS NULL)."""
        gid = to_id(guild_id)
        if gid is None:
            return []
        rows = supabase_client.select(
            "qotd_questions",
            filters={"guild_id": gid},
            order_by="position",
        )
        return [r for r in rows if r.get("asked_at") is None]

    def qotd_history(self, guild_id: int, limit: int = 50) -> list[dict[str, Any]]:
        gid = to_id(guild_id)
        if gid is None:
            return []
        rows = supabase_client.select("qotd_questions", filters={"guild_id": gid}, order_by="position")
        asked = [r for r in rows if r.get("asked_at") is not None]
        asked.sort(key=lambda r: str(r.get("asked_at")), reverse=True)
        return asked[:limit]

    def qotd_add(self, guild_id: int, question: str, *, created_by: Any = None) -> dict[str, Any]:
        gid = to_id(guild_id)
        if gid is None:
            raise ConfigError("invalid guild id")

        cleaned = (question or "").strip()
        if not cleaned:
            raise ConfigError("question must not be blank")
        if len(cleaned) > MAX_QUESTION_LENGTH:
            raise ConfigError(f"question must be at most {MAX_QUESTION_LENGTH} characters")

        self._ensure_settings_row(gid)
        queue = self.qotd_queue(gid)
        next_position = (max((int(r.get("position") or 0) for r in queue), default=0)) + 1

        payload: dict[str, Any] = {
            "guild_id": gid,
            "question": cleaned,
            "position": next_position,
        }
        creator_id = to_id(created_by) if created_by is not None else None
        # created_by is a uuid column; a Discord int is not a uuid. Only set it
        # when the caller supplied a real uuid.
        if isinstance(created_by, str) and _looks_like_uuid(created_by):
            payload["created_by"] = created_by
        elif creator_id is not None:
            log.debug("Ignoring non-uuid created_by for QOTD (%r)", created_by)

        rows = supabase_client.upsert("qotd_questions", payload)
        if not rows:
            raise ConfigError("failed to add question")
        log.info("QOTD question added for guild %s at position %s", gid, next_position)
        return rows[0]

    def qotd_pop_next(self, guild_id: int) -> dict[str, Any] | None:
        """Return the next queued question without consuming it."""
        queue = self.qotd_queue(guild_id)
        return queue[0] if queue else None

    def qotd_mark_asked(self, guild_id: int, question_id: int, message_id: int | None = None) -> None:
        """Mark a question as asked. Soft-delete: history is preserved."""
        gid = to_id(guild_id)
        if gid is None:
            raise ConfigError("invalid guild id")
        changes: dict[str, Any] = {"asked_at": datetime.now(timezone.utc).isoformat()}
        if message_id is not None:
            changes["asked_message_id"] = to_id(message_id)
        supabase_client.update("qotd_questions", changes, filters={"id": question_id})
        log.info("QOTD question %s marked asked in guild %s", question_id, gid)

    def qotd_delete(self, guild_id: int, question_id: int) -> None:
        gid = to_id(guild_id)
        if gid is None:
            raise ConfigError("invalid guild id")
        supabase_client.delete("qotd_questions", filters={"id": question_id, "guild_id": gid})
        self.qotd_renumber(gid)

    def qotd_move(self, guild_id: int, question_id: int, new_position: int) -> None:
        """Reorder the queue. Runs the whole reorder in one SQL transaction.

        The Postgres helper is SECURITY INVOKER and checks can_manage_guild(),
        which is meaningless for service_role (auth.uid() is NULL). The bot
        therefore performs the equivalent reorder directly.
        """
        gid = to_id(guild_id)
        if gid is None:
            raise ConfigError("invalid guild id")

        queue = self.qotd_queue(gid)
        if not queue:
            raise ConfigError("the queue is empty")

        target_index = next(
            (i for i, r in enumerate(queue) if int(r["id"]) == int(question_id)), None
        )
        if target_index is None:
            raise ConfigError(f"question {question_id} is not in the queue")

        clamped = max(1, min(int(new_position), len(queue)))
        item = queue.pop(target_index)
        queue.insert(clamped - 1, item)

        for index, row in enumerate(queue, start=1):
            if int(row.get("position") or 0) != index:
                supabase_client.update(
                    "qotd_questions", {"position": index}, filters={"id": int(row["id"])}
                )
        log.info("QOTD queue reordered for guild %s", gid)

    def qotd_renumber(self, guild_id: int) -> None:
        """Compact positions so ordering stays gap-free after removals."""
        gid = to_id(guild_id)
        if gid is None:
            return
        queue = self.qotd_queue(gid)
        for index, row in enumerate(queue, start=1):
            if int(row.get("position") or 0) != index:
                supabase_client.update(
                    "qotd_questions", {"position": index}, filters={"id": int(row["id"])}
                )

    def qotd_mark_fired(self, guild_id: int) -> None:
        """Stamp qotd_last_fired_at. The restart double-fire guard."""
        self._update_settings(
            guild_id, {"qotd_last_fired_at": datetime.now(timezone.utc).isoformat()}
        )


# --- helpers ----------------------------------------------------------------


def _check_range(label: str, value: Any, minimum: Any, maximum: Any) -> None:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be a number") from exc
    if numeric < minimum or numeric > maximum:
        raise ConfigError(f"{label} must be between {minimum} and {maximum}")


def _looks_like_uuid(value: str) -> bool:
    parts = value.split("-")
    return len(parts) == 5 and [len(p) for p in parts] == [8, 4, 4, 4, 12]


def _source() -> str:
    """Best-effort attribution of the current write. Helps answer 'who changed this?'."""
    import inspect

    for frame_info in inspect.stack()[2:]:
        module = frame_info.frame.f_globals.get("__name__", "")
        if module.startswith("cogs."):
            return module
        if module.startswith("db."):
            return f"{module} (realtime)"
    return "services"


def build_service() -> ConfigService:
    return ConfigService()


__all__ = ["ConfigService", "ConfigError", "build_service", "DEFAULT_SETTINGS"]
