"""Operator bridge for the owner-only admin dashboard.

Three jobs, all of them things only the live bot process can do:

  1. guild stats    - snapshot member/online counts so the admin sees real sizes
  2. DM outbox      - send the messages the operator queued, and record the outcome
  3. admin requests - run jobs the Edge Function queued (command re-sync)

Incoming DMs are recorded by ``cogs/admin_cog.py`` (a Discord event) which calls
:meth:`AdminBridge.record_dm` here.

Everything is best-effort and bounded: a failing tick logs and retries on the
next one, because none of these is worth crashing the bot over. The bridge owns
no Discord token of its own - it runs inside the bot, which is the only actor
allowed to send as the bot.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Any

import discord

from db import supabase_client
from utils.logging_setup import get_logger

log = get_logger("services.admin")

# How often the bridge wakes up. It does three cheap things, so 90s keeps the DM
# console responsive without hammering the database.
TICK_SECONDS = 90.0

# Publish guild sizes every Nth tick (~6 minutes at the default tick). Member
# counts move slowly and each snapshot is a few writes per guild.
STATS_EVERY_TICKS = 4

# Bounded batches so one busy queue cannot stall the event loop.
OUTBOX_BATCH = 10
REQUEST_BATCH = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AdminBridge:
    """Background worker that connects the bot to the admin dashboard."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._ticks = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="admin-bridge")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def _run(self) -> None:
        # Guilds are not populated until the gateway is ready.
        with contextlib.suppress(Exception):
            await self.bot.wait_until_ready()

        while not self._stopping.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("Admin bridge tick failed: %s", exc, exc_info=True)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_SECONDS)

    async def _tick(self) -> None:
        await self._pump_outbox()
        await self._pump_requests()
        self._ticks += 1
        # Sample sizes immediately on the first tick, then every Nth.
        if self._ticks == 1 or self._ticks % STATS_EVERY_TICKS == 0:
            await self._publish_stats()

    # -- incoming DMs ------------------------------------------------------

    async def record_dm(self, message: discord.Message) -> None:
        """Store one incoming DM. Called from cogs/admin_cog.py."""
        attachments = [
            {
                "filename": a.filename,
                "url": a.url,
                "content_type": a.content_type,
                "size": a.size,
            }
            for a in message.attachments
        ]
        payload = {
            "discord_user_id": message.author.id,
            "username": message.author.name,
            "display_name": getattr(message.author, "display_name", None),
            "content": message.content or "",
            "attachments": attachments,
            "message_id": message.id,
        }
        try:
            await asyncio.to_thread(supabase_client.insert, "bot_dms", payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not record DM from %s: %s", message.author.id, exc)

    # -- outbound DMs ------------------------------------------------------

    async def _pump_outbox(self) -> None:
        rows = await asyncio.to_thread(
            supabase_client.select,
            "bot_dm_outbox",
            "id,recipient_id,content",
            filters={"status": "pending"},
            order_by="created_at",
            limit=OUTBOX_BATCH,
        )
        for row in rows or []:
            await self._send_outbox_row(row)

    async def _send_outbox_row(self, row: dict[str, Any]) -> None:
        row_id = row["id"]
        recipient_id = int(row["recipient_id"])
        content = row.get("content") or ""

        try:
            user = self.bot.get_user(recipient_id) or await self.bot.fetch_user(recipient_id)
            await user.send(content)
        except discord.Forbidden:
            await self._mark_outbox(row_id, "failed", "The user does not accept DMs from this bot.")
        except discord.NotFound:
            await self._mark_outbox(row_id, "failed", "That user no longer exists.")
        except Exception as exc:  # noqa: BLE001
            await self._mark_outbox(row_id, "failed", str(exc)[:400])
        else:
            await self._mark_outbox(row_id, "sent", None)

    async def _mark_outbox(self, row_id: int, status: str, error: str | None) -> None:
        payload: dict[str, Any] = {"status": status, "error": error}
        if status == "sent":
            payload["sent_at"] = _now()
        try:
            await asyncio.to_thread(
                supabase_client.update, "bot_dm_outbox", payload, filters={"id": row_id}
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not update DM outbox row %s: %s", row_id, exc)

    # -- queued requests ---------------------------------------------------

    async def _pump_requests(self) -> None:
        rows = await asyncio.to_thread(
            supabase_client.select,
            "admin_requests",
            "id,kind,payload",
            filters={"status": "pending"},
            order_by="created_at",
            limit=REQUEST_BATCH,
        )
        for row in rows or []:
            await self._run_request(row)

    async def _run_request(self, row: dict[str, Any]) -> None:
        row_id = row["id"]
        kind = row.get("kind")
        status, result = "failed", f"unknown request kind: {kind}"
        try:
            if kind == "resync_commands":
                # Global + dev guilds; the same path a restart uses.
                await self.bot._sync_commands()  # type: ignore[attr-defined]
                status, result = "done", "Command tree re-synced (global + dev guilds)."
        except Exception as exc:  # noqa: BLE001
            status, result = "failed", str(exc)[:400]

        try:
            await asyncio.to_thread(
                supabase_client.update,
                "admin_requests",
                {"status": status, "result": result, "processed_at": _now()},
                filters={"id": row_id},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not finish admin request %s: %s", row_id, exc)

    # -- guild stats -------------------------------------------------------

    async def _publish_stats(self) -> None:
        # Presence is a privileged intent. Without it every member reads as
        # offline, so the honest value is NULL rather than a false zero.
        presences = bool(self.bot.intents.presences)
        if not presences:
            log.debug("Presence intent is off; publishing member counts only.")

        for guild in self.bot.guilds:
            payload: dict[str, Any] = {
                "id": guild.id,
                "member_count": guild.member_count,
                "online_count": self._count_online(guild) if presences else None,
                "stats_updated_at": _now(),
            }
            try:
                await asyncio.to_thread(
                    supabase_client.upsert, "guilds", payload, on_conflict="id"
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not publish stats for guild %s: %s", guild.id, exc)

    @staticmethod
    def _count_online(guild: discord.Guild) -> int:
        return sum(1 for m in guild.members if m.status is not discord.Status.offline)


__all__ = ["AdminBridge"]
