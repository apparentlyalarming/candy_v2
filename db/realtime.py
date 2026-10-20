"""Realtime listener: keeps the config cache fresh.

Design rule (from buildplan.txt): Realtime is a SIGNAL, not a diff.
On any change event we re-fetch the affected row in full and replace the cache
entry. We never merge payload diffs — a bot redeploy mid-edit would desync them.

supabase-py's realtime support has historically lagged the JS client, so this
module degrades gracefully:
  1. Try the supabase-py realtime API if this version exposes it.
  2. Fall back to a polling loop that re-reads changed rows.
Either way the rest of the bot only sees populated cache entries.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Callable

from db import config_cache, supabase_client
from utils.logging_setup import get_logger

log = get_logger("db.realtime")

# Tables whose changes must refresh the cache.
WATCHED_TABLES = ("guild_settings",)

# Polling fallback interval (seconds).
POLL_INTERVAL = 15.0


class RealtimeListener:
    """Keeps ``config_cache`` in sync with Postgres.

    Usage from the bot::

        listener = RealtimeListener(on_settings_changed=my_callback)
        listener.start()          # non-blocking, runs as an asyncio task
        ...
        await listener.stop()
    """

    def __init__(
        self,
        *,
        on_settings_changed: Callable[[int, dict[str, Any]], Any] | None = None,
    ) -> None:
        self._on_settings_changed = on_settings_changed
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._mode: str = "idle"

    # --- lifecycle ---------------------------------------------------------

    @property
    def mode(self) -> str:
        """'realtime', 'polling', or 'idle' — useful for /status output."""
        return self._mode

    def start(self) -> None:
        """Launch the listener as a background asyncio task."""
        if self._task is not None and not self._task.done():
            log.debug("Realtime listener already running")
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="realtime-listener")

    async def stop(self) -> None:
        """Signal the listener to stop and await its shutdown."""
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._mode = "idle"
        log.info("Realtime listener stopped")

    # --- main loop ---------------------------------------------------------

    async def _run(self) -> None:
        await asyncio.sleep(1)  # let startup finish wiring things up

        try:
            await self._run_realtime()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "Realtime subscription unavailable (%s: %s). Falling back to polling.",
                type(exc).__name__,
                exc,
            )
            self._mode = "polling"
            try:
                await self._run_polling()
            except asyncio.CancelledError:
                raise
            except Exception as poll_exc:
                log.error("Polling fallback also failed: %s", poll_exc, exc_info=True)

    # --- strategies --------------------------------------------------------

    async def _run_realtime(self) -> None:
        """Attempt a genuine postgres_changes subscription."""
        client = supabase_client.get_client()

        # Different supabase-py versions expose realtime differently. Probe for
        # the pieces we need rather than assuming, so a version bump degrades
        # to polling instead of crashing the bot.
        realtime = getattr(client, "realtime", None)
        if realtime is None:
            raise RuntimeError("client.realtime is not exposed by this supabase-py version")

        channel = None
        channel_factory = getattr(realtime, "channel", None)
        if channel_factory is None:
            raise RuntimeError("realtime.channel() is not available in this supabase-py version")

        channel = channel_factory("candy_v2_config")

        for table in WATCHED_TABLES:
            on_method = getattr(channel, "on_postgres_changes", None)
            if on_method is None:
                raise RuntimeError("channel.on_postgres_changes() is not available")

            def _handler(payload: dict[str, Any], _table: str = table) -> None:
                # Called from the realtime thread; hand off to the event loop.
                asyncio.create_task(self._handle_payload(payload, _table))

            on_method(
                event="*",
                schema="public",
                table=table,
                callback=_handler,
            )

        subscribe = getattr(channel, "subscribe", None)
        if subscribe is None:
            raise RuntimeError("channel.subscribe() is not available")

        await subscribe()
        self._mode = "realtime"
        log.info("Subscribed to realtime changes on: %s", ", ".join(WATCHED_TABLES))

        # Stay alive until asked to stop.
        await self._stopping.wait()

    async def _run_polling(self) -> None:
        """Fallback: periodically re-read all settings rows and diff them.

        Correct rather than clever: we detect change by comparing a cheap
        fingerprint, and only re-fetch rows that actually differ.
        """
        log.info("Polling config for changes every %.0fs", POLL_INTERVAL)
        fingerprints: dict[int, str] = {}

        while not self._stopping.is_set():
            try:
                rows = await asyncio.to_thread(
                    supabase_client.select, "guild_settings"
                )
                for row in rows:
                    from utils.discord_ids import to_id

                    guild_id = to_id(row.get("guild_id"))
                    if guild_id is None:
                        continue
                    fingerprint = self._fingerprint(row)
                    if fingerprints.get(guild_id) != fingerprint:
                        fingerprints[guild_id] = fingerprint
                        config_cache.put(row)
                        await self._notify(guild_id, row)
            except Exception as exc:
                log.warning("Poll cycle failed: %s", exc)

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=POLL_INTERVAL)

    # --- helpers -----------------------------------------------------------

    async def _handle_payload(self, payload: dict[str, Any], table: str) -> None:
        """Re-fetch the affected row in full, then update the cache."""
        data = payload.get("data") or payload.get("new") or {}
        guild_id_raw = data.get("guild_id")
        event = payload.get("eventType") or payload.get("event") or "unknown"

        from utils.discord_ids import to_id

        guild_id = to_id(guild_id_raw)
        if guild_id is None:
            log.debug("Realtime %s on %s without a guild_id; ignoring", event, table)
            return

        log.info("Realtime %s on %s for guild %s", event, table, guild_id)

        try:
            row = await asyncio.to_thread(
                supabase_client.select_one,
                table,
                filters={"guild_id": guild_id},
            )
        except Exception as exc:
            # Do not silently keep stale config: log loudly.
            log.error(
                "Realtime signal for guild %s arrived but re-fetch failed: %s",
                guild_id,
                exc,
                exc_info=True,
            )
            return

        if row is None:
            # Row was deleted (e.g. guild removed). Drop from cache.
            config_cache.drop(guild_id)
            await self._notify(guild_id, {})
            return

        config_cache.put(row)
        await self._notify(guild_id, row)

    async def _notify(self, guild_id: int, row: dict[str, Any]) -> None:
        if self._on_settings_changed is None:
            return
        try:
            result = self._on_settings_changed(guild_id, row)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            log.error("on_settings_changed callback failed: %s", exc, exc_info=True)

    @staticmethod
    def _fingerprint(row: dict[str, Any]) -> str:
        """Cheap change detector for the polling fallback."""
        stable_keys = sorted(k for k in row.keys() if k not in {"updated_at"})
        return repr([(k, row[k]) for k in stable_keys])


def load_initial_cache() -> int:
    """Bulk-load all guild settings at startup. Blocking; call via to_thread."""
    rows = supabase_client.select("guild_settings")
    return config_cache.load_all(rows)


__all__ = ["RealtimeListener", "load_initial_cache", "WATCHED_TABLES", "POLL_INTERVAL"]
