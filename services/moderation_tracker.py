"""Moderation tracking for the monthly report.

Two sources, deliberately split by action:

  audit log   ban, unban, kick, timeout
  Dyno embed  warn / warning ONLY

Why not parse everything from Dyno? A Dyno ban/kick embed duplicates an audit
log entry, so parsing both would double-count. The audit log is also the only
source that records native Discord actions. The cost of that choice is that an
action performed *through* Dyno names Dyno as the audit actor; the ``via``
column records that honestly instead of guessing which human clicked it.

Warnings are the exception: Dyno issues them without a Discord audit entry, so
the embed is the only record. The parser is deterministic and stores the raw
embed when it cannot confidently extract a target, so a later re-parse or a
human review can fix it. A Groq fallback is deliberately not used here.

Audit entries expire after 45 days. The poller advances a per-guild cursor only
after the corresponding rows are written, so a crash or restart re-reads rather
than skips. The unique index on (guild_id, source, external_id) makes the
re-read idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from db import supabase_client
from services.config_service import ConfigService
from utils.logging_setup import get_logger

log = get_logger("services.moderation")

# `Case 169 | Ban | spech_22`
DYNO_CASE_RE = re.compile(r"^Case\s+(\d+)\s*\|\s*([^|]+?)\s*\|\s*(.+)$", re.IGNORECASE)
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
FOOTER_ID_RE = re.compile(r"ID:\s*(\d+)")
WARN_ACTIONS = {"warn", "warning"}

# Audit pages are 100 entries each. 20 pages = 2000 entries, enough for a
# month of normal moderation without letting a first-run backfill run forever.
AUDIT_PAGE_SIZE = 100
MAX_AUDIT_PAGES = 20

# A timeout is detected by on_member_update, then correlated to the most recent
# member_update audit entry for that target. Keep the window tight: a role
# change a minute earlier must not be mistaken for the timeout.
TIMEOUT_AUDIT_WINDOW = timedelta(seconds=45)

AUDIT_ACTIONS = {
    discord.AuditLogAction.ban: "ban",
    discord.AuditLogAction.unban: "unban",
    discord.AuditLogAction.kick: "kick",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _target_name(target: Any) -> str | None:
    if target is None:
        return None
    if isinstance(target, (discord.User, discord.Member)):
        return str(target)
    name = getattr(target, "name", None)
    if name:
        return str(name)
    # discord.Object: str() is "<Object id=...>", which is not useful in a
    # report. The id is stored separately, so leave the name NULL.
    return None


def _strip_mention(text: str) -> str:
    return USER_MENTION_RE.sub("", text or "").strip().lstrip("@").strip()


class ModerationTracker:
    """Polls audit logs and parses Dyno warning embeds."""

    def __init__(
        self,
        bot: discord.Client,
        config_service: ConfigService,
        *,
        poll_seconds: int = 600,
        backfill_days: int = 30,
    ) -> None:
        self.bot = bot
        self._config = config_service
        self._poll_seconds = max(120, int(poll_seconds))
        self._backfill_days = max(0, min(45, int(backfill_days)))

        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._polling = asyncio.Lock()
        self._cursors: dict[int, int | None] = {}
        self._permission_warned: set[int] = set()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="moderation-poller")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        log.info("Moderation poller stopped")

    async def _run(self) -> None:
        with contextlib.suppress(Exception):
            await self.bot.wait_until_ready()
        await asyncio.sleep(15)
        log.info("Moderation poller started (every %ds)", self._poll_seconds)

        while not self._stopping.is_set():
            try:
                await self.poll_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Moderation poll failed: %s", exc, exc_info=True)

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._poll_seconds
                )

    async def poll_all(self) -> None:
        if self._polling.locked():
            return
        async with self._polling:
            for guild in list(self.bot.guilds):
                try:
                    await self._poll_guild(guild)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "Audit-log poll failed for guild %s: %s", guild.id, exc
                    )

    # -- audit log ---------------------------------------------------------

    async def _poll_guild(self, guild: discord.Guild) -> None:
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            if guild.id not in self._permission_warned:
                self._permission_warned.add(guild.id)
                log.warning(
                    "Guild %s: no View Audit Log permission; moderation actions "
                    "will not be recorded",
                    guild.id,
                )
            return

        cursor = await self._get_cursor(guild.id)
        after: discord.Object
        newest_id = cursor

        if cursor is None:
            if self._backfill_days > 0:
                start = _utcnow() - timedelta(days=self._backfill_days)
                after = discord.Object(id=discord.utils.time_snowflake(start))
                log.info(
                    "Guild %s: first audit poll, backfilling %d day(s)",
                    guild.id,
                    self._backfill_days,
                )
            else:
                # No backfill: anchor to the newest entry and start from here.
                newest = await self._newest_entry(guild)
                anchor = newest.id if newest is not None else discord.utils.time_snowflake(_utcnow())
                await self._save_cursor(guild.id, anchor)
                return
        else:
            after = discord.Object(id=cursor)

        rows: list[dict[str, Any]] = []
        seen = 0
        limit = AUDIT_PAGE_SIZE * MAX_AUDIT_PAGES

        try:
            async for entry in guild.audit_logs(
                limit=limit,
                after=after,
                oldest_first=True,
            ):
                seen += 1
                if newest_id is None or entry.id > newest_id:
                    newest_id = entry.id

                row = self._audit_row(guild, entry)
                if row is not None:
                    rows.append(row)

                if seen >= limit:
                    break
        except discord.Forbidden:
            if guild.id not in self._permission_warned:
                self._permission_warned.add(guild.id)
                log.warning("Guild %s: audit log access denied", guild.id)
            return
        except discord.HTTPException as exc:
            log.warning("Guild %s: audit log request failed: %s", guild.id, exc)
            return

        if rows:
            try:
                await asyncio.to_thread(self._upsert_rows, rows)
            except Exception as exc:  # noqa: BLE001 - do not advance the cursor
                log.warning(
                    "Guild %s: could not write %d moderation row(s): %s",
                    guild.id,
                    len(rows),
                    exc,
                )
                return

        # Only now is it safe to advance. Entries we ignored (role changes,
        # message deletes, ...) still advance the cursor; re-reading them would
        # cost API calls for no benefit.
        if newest_id is not None and newest_id != cursor:
            await self._save_cursor(guild.id, newest_id)

    async def _newest_entry(self, guild: discord.Guild) -> discord.AuditLogEntry | None:
        try:
            async for entry in guild.audit_logs(limit=1, oldest_first=False):
                return entry
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.debug("Could not read newest audit entry for guild %s: %s", guild.id, exc)
        return None

    def _audit_row(
        self, guild: discord.Guild, entry: discord.AuditLogEntry
    ) -> dict[str, Any] | None:
        kind = AUDIT_ACTIONS.get(entry.action)
        if kind is None:
            # member_update is handled by the timeout path, not here: the diff
            # does not reliably expose timed_out_until across discord.py
            # versions, and on_member_update sees the transition directly.
            return None

        config = self._config.get(guild.id)
        actor = entry.user
        actor_id = actor.id if actor is not None else None
        via = "dyno" if actor_id is not None and actor_id == config.dyno_user_id else "native"

        target = entry.target
        reason = (entry.reason or "").strip() or None
        if reason is not None:
            reason = reason[:1000]

        return {
            "guild_id": guild.id,
            "occurred_at": entry.created_at.isoformat(),
            "kind": kind,
            "target_id": getattr(target, "id", None),
            "target_name": _target_name(target),
            "moderator_id": actor_id,
            "moderator_name": str(actor) if actor is not None else None,
            "reason": reason,
            "source": "audit",
            "via": via,
            "external_id": str(entry.id),
            "case_number": None,
            "raw_embed": None,
            "needs_review": False,
        }

    # -- timeouts (gateway + targeted audit lookup) ------------------------

    async def record_timeout(
        self, member: discord.Member, timed_out_until: datetime | None
    ) -> None:
        """Record a timeout detected by ``on_member_update``.

        The audit entry is fetched by target and time window. If it cannot be
        found, a synthetic external_id is used so the action is still counted;
        the moderator then reads as unknown rather than being invented.
        """
        if timed_out_until is None:
            return

        guild = member.guild
        config = self._config.get(guild.id)
        now = _utcnow()
        entry = await self._find_timeout_entry(guild, member.id, now)

        actor = entry.user if entry is not None else None
        actor_id = actor.id if actor is not None else None
        via = "dyno" if actor_id is not None and actor_id == config.dyno_user_id else "native"
        reason = None
        if entry is not None and entry.reason:
            reason = entry.reason.strip()[:1000] or None

        if entry is not None:
            external_id = str(entry.id)
            occurred_at = entry.created_at
        else:
            # The timeout end is part of the key so two separate timeouts of
            # the same member are distinct events.
            external_id = f"timeout:{member.id}:{int(timed_out_until.timestamp())}"
            occurred_at = now

        row = {
            "guild_id": guild.id,
            "occurred_at": occurred_at.isoformat(),
            "kind": "timeout",
            "target_id": member.id,
            "target_name": str(member),
            "moderator_id": actor_id,
            "moderator_name": str(actor) if actor is not None else None,
            "reason": reason,
            "source": "audit",
            "via": via,
            "external_id": external_id,
            "case_number": None,
            "raw_embed": None,
            "needs_review": entry is None,
        }

        try:
            await asyncio.to_thread(self._upsert_rows, [row])
            log.info(
                "Recorded timeout for %s in guild %s (moderator %s)",
                member.id,
                guild.id,
                actor_id or "unknown",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not record timeout for %s: %s", member.id, exc)

    async def _find_timeout_entry(
        self, guild: discord.Guild, target_id: int, now: datetime
    ) -> discord.AuditLogEntry | None:
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(
                limit=10,
                action=discord.AuditLogAction.member_update,
                oldest_first=False,
            ):
                if entry.target is None or entry.target.id != target_id:
                    continue
                if now - entry.created_at > TIMEOUT_AUDIT_WINDOW:
                    break
                return entry
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.debug("Timeout audit lookup failed for guild %s: %s", guild.id, exc)
        return None

    # -- Dyno warnings -----------------------------------------------------

    async def handle_modlog_message(self, message: discord.Message) -> None:
        """Parse one message in the configured mod-log channel."""
        guild = message.guild
        if guild is None or not message.author.bot:
            return

        config = self._config.get(guild.id)
        if config.moderation_log_channel_id is None:
            return
        if message.channel.id != config.moderation_log_channel_id:
            return
        if message.author.id != config.dyno_user_id:
            return

        rows: list[dict[str, Any]] = []
        for embed in message.embeds:
            row = self.parse_dyno_embed(embed, message, guild, config)
            if row is not None:
                rows.append(row)

        if not rows:
            return
        try:
            await asyncio.to_thread(self._upsert_rows, rows)
            log.info(
                "Recorded %d Dyno warning(s) from message %s in guild %s",
                len(rows),
                message.id,
                guild.id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not record Dyno warning(s) from %s: %s", message.id, exc)

    def parse_dyno_embed(
        self,
        embed: discord.Embed,
        message: discord.Message,
        guild: discord.Guild,
        config: Any,
    ) -> dict[str, Any] | None:
        """Turn a Dyno mod-log embed into a row, or None if it is not a warning.

        Public so tests can feed a stored embed dict without a live message.
        """
        title = (embed.title or "").strip()
        match = DYNO_CASE_RE.match(title)
        if match is None:
            return None

        case_number = int(match.group(1))
        action = match.group(2).strip().lower()
        if action not in WARN_ACTIONS:
            # Ban/kick are already in the audit log; parsing them here would
            # double-count. Ignore every non-warning case.
            return None

        title_target = match.group(3).strip()
        fields = {field.name.strip().lower(): field.value for field in embed.fields}

        user_value = fields.get("user", "")
        target_id = self._first_id(user_value)
        target_name = title_target or _strip_mention(user_value) or None

        footer_text = (embed.footer.text or "") if embed.footer else ""
        if target_id is None:
            footer_match = FOOTER_ID_RE.search(footer_text)
            if footer_match:
                target_id = int(footer_match.group(1))

        moderator_value = fields.get("moderator", "")
        moderator_id = self._first_id(moderator_value)
        moderator_name = _strip_mention(moderator_value) or None
        if moderator_id is None and moderator_name:
            member = self._find_member(guild, moderator_name)
            if member is not None:
                moderator_id = member.id
                moderator_name = str(member)

        reason = (fields.get("reason") or "").strip() or None
        if reason is not None:
            reason = reason[:1000]

        return {
            "guild_id": guild.id,
            "occurred_at": message.created_at.isoformat(),
            "kind": "warn",
            "target_id": target_id,
            "target_name": target_name,
            "moderator_id": moderator_id,
            "moderator_name": moderator_name,
            "reason": reason,
            "source": "dyno_embed",
            "via": "dyno",
            "external_id": str(message.id),
            "case_number": case_number,
            "raw_embed": embed.to_dict(),
            # A warning with no identifiable target is still stored; a human
            # can fix it later rather than the action silently disappearing.
            "needs_review": target_id is None,
        }

    @staticmethod
    def _first_id(text: str) -> int | None:
        match = USER_MENTION_RE.search(text or "")
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _find_member(guild: discord.Guild, name: str) -> discord.Member | None:
        needle = name.strip().lower()
        if not needle:
            return None
        for member in guild.members:
            if member.name.lower() == needle or member.display_name.lower() == needle:
                return member
        return None

    # -- persistence -------------------------------------------------------

    @staticmethod
    def _upsert_rows(rows: list[dict[str, Any]]) -> None:
        supabase_client.upsert(
            "guild_moderation_events",
            rows,
            on_conflict="guild_id,source,external_id",
        )

    async def _get_cursor(self, guild_id: int) -> int | None:
        if guild_id in self._cursors:
            return self._cursors[guild_id]
        try:
            row = await asyncio.to_thread(
                supabase_client.select_one,
                "guild_moderation_cursors",
                filters={"guild_id": guild_id},
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not read moderation cursor for %s: %s", guild_id, exc)
            return None
        cursor = int(row["last_audit_id"]) if row and row.get("last_audit_id") else None
        self._cursors[guild_id] = cursor
        return cursor

    async def _save_cursor(self, guild_id: int, last_audit_id: int) -> None:
        try:
            await asyncio.to_thread(
                supabase_client.upsert,
                "guild_moderation_cursors",
                {"guild_id": guild_id, "last_audit_id": last_audit_id},
                on_conflict="guild_id",
            )
            self._cursors[guild_id] = last_audit_id
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not save moderation cursor for guild %s: %s", guild_id, exc)


__all__ = ["ModerationTracker", "DYNO_CASE_RE"]
