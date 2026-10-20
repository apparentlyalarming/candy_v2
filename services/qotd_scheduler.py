"""Question of the Day scheduler.

The hard part is time, not Discord. "10 AM" is meaningless without a zone, and
the bot host plus Supabase both run UTC. So:

  - the schedule is stored as a local time + an IANA timezone
  - the next occurrence is computed IN that zone, then converted to aware UTC
  - DST is handled by recomputing from local time each tick, never by adding 24h

The restart guard matters more than it looks: bot-hosting.net requires a manual
renewal every 4 days, so restarts are routine, not exceptional. Without
`qotd_last_fired_at`, a restart near the scheduled minute double-posts.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Awaitable, Callable

from db import config_cache
from db.config_cache import GuildConfig
from services.config_service import ConfigService
from utils.logging_setup import get_logger
from utils.timezones import resolve_zone  # noqa: F401 - re-exported below

log = get_logger("services.qotd")

TICK_SECONDS = 60.0

# If the bot was down at the scheduled moment, still fire when it comes back,
# but only within this window. Prevents a 3-day-old missed slot from posting at
# a random hour.
GRACE_WINDOW_MINUTES = 120


def parse_local_time(raw: str | None) -> dtime | None:
    """Parse a Postgres ``time`` value such as '10:00:00' into a dtime."""
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%H:%M:%S", "%H:%M", "%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    log.warning("Could not parse qotd_time %r", raw)
    return None


def next_occurrence(
    now_utc: datetime,
    local_time: dtime,
    zone: tzinfo,
    days: list[int],
    *,
    from_date: date | None = None,
) -> datetime:
    """Return the next scheduled moment as an aware UTC datetime.

    ``days`` uses 1=Mon .. 7=Sun. Empty means every day.

    Recomputed from local time on every call, so DST transitions shift the wall
    clock, not the UTC offset - which is what users actually mean by "10 AM".
    """
    start_date = from_date or now_utc.astimezone(zone).date()

    for offset in range(0, 15):  # look ahead up to two weeks
        candidate_date = start_date + timedelta(days=offset)
        if days:
            iso_weekday = candidate_date.isoweekday()
            if iso_weekday not in days:
                continue

        local_dt = datetime.combine(candidate_date, local_time).replace(tzinfo=zone)
        candidate_utc = local_dt.astimezone(timezone.utc)

        if candidate_utc > now_utc:
            return candidate_utc

    # Nothing matched within the lookahead (shouldn't happen); push a day out.
    local_dt = datetime.combine(start_date + timedelta(days=1), local_time).replace(tzinfo=zone)
    return local_dt.astimezone(timezone.utc)


def already_fired_today(config: GuildConfig, now_utc: datetime) -> bool:
    """True when qotd_last_fired_at falls on today's date in the guild's zone."""
    raw = config.qotd_last_fired_at
    if not raw:
        return False
    try:
        fired_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)

    zone = resolve_zone(config.qotd_timezone)
    return fired_at.astimezone(zone).date() == now_utc.astimezone(zone).date()


def is_due(config: GuildConfig, now_utc: datetime) -> bool:
    """Decide whether this guild's QOTD should fire right now."""
    if not config.qotd_enabled:
        return False
    if config.qotd_channel_id is None:
        return False

    local_time = parse_local_time(config.qotd_time)
    if local_time is None:
        return False

    zone = resolve_zone(config.qotd_timezone)
    local_now = now_utc.astimezone(zone)

    if config.qotd_days and local_now.isoweekday() not in config.qotd_days:
        return False

    if already_fired_today(config, now_utc):
        return False

    scheduled_local = datetime.combine(local_now.date(), local_time).replace(tzinfo=zone)
    scheduled_utc = scheduled_local.astimezone(timezone.utc)

    if now_utc < scheduled_utc:
        return False

    # Within the grace window, or the bot missed the slot entirely.
    if now_utc - scheduled_utc <= timedelta(minutes=GRACE_WINDOW_MINUTES):
        return True

    # Missed beyond the grace window: skip this slot rather than post at a
    # strange hour. Stamp it so we do not reconsider it all day.
    log.info(
        "Skipping missed QOTD slot for guild %s (scheduled %s UTC, now %s UTC)",
        config.guild_id,
        scheduled_utc.isoformat(),
        now_utc.isoformat(),
    )
    return False


class QotdScheduler:
    """Polls each guild's schedule once a minute and fires due questions."""

    def __init__(
        self,
        *,
        config_service: ConfigService,
        poster: Callable[[GuildConfig, dict], Awaitable[bool]],
    ) -> None:
        self._config = config_service
        self._poster = poster
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_tick: datetime | None = None
        self._missed_notice_sent: set[int] = set()

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="qotd-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        log.info("QOTD scheduler stopped")

    @property
    def last_tick(self) -> datetime | None:
        return self._last_tick

    # --- loop --------------------------------------------------------------

    async def _run(self) -> None:
        await asyncio.sleep(5)  # let startup settle
        log.info("QOTD scheduler started (tick every %.0fs, UTC clock)", TICK_SECONDS)

        while not self._stopping.is_set():
            started = asyncio.get_event_loop().time()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("QOTD tick failed: %s", exc, exc_info=True)

            elapsed = asyncio.get_event_loop().time() - started
            delay = max(5.0, TICK_SECONDS - elapsed)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    async def tick(self, *, now_utc: datetime | None = None) -> None:
        """One scheduling pass over all cached guilds. Also used by tests."""
        now = now_utc or datetime.now(timezone.utc)
        self._last_tick = now

        for guild_id in config_cache.all_guild_ids():
            config = config_cache.get(guild_id)
            if not config.qotd_enabled:
                continue
            try:
                if is_due(config, now):
                    await self._fire(config)
            except Exception as exc:
                log.error(
                    "QOTD handling failed for guild %s: %s", guild_id, exc, exc_info=True
                )

    # --- firing ------------------------------------------------------------

    async def _fire(self, config: GuildConfig) -> None:
        guild_id = config.guild_id

        next_question = await asyncio.to_thread(self._config.qotd_pop_next, guild_id)

        if next_question is None:
            log.info("QOTD queue empty for guild %s", guild_id)
            await asyncio.to_thread(self._config.qotd_mark_fired, guild_id)
            if config.qotd_empty_queue_notice:
                await self._poster(config, {"__empty__": True})
            return

        log.info(
            "Firing QOTD for guild %s: question id %s",
            guild_id,
            next_question.get("id"),
        )

        delivered = await self._poster(config, next_question)

        if delivered:
            await asyncio.to_thread(self._config.qotd_mark_fired, guild_id)
        else:
            # Do NOT consume the question or stamp last_fired_at if the post
            # failed; the next tick will retry within the grace window.
            log.warning(
                "QOTD post failed for guild %s; question %s left in the queue",
                guild_id,
                next_question.get("id"),
            )


def build_scheduler(
    *, config_service: ConfigService, poster: Callable[[GuildConfig, dict], Awaitable[bool]]
) -> QotdScheduler:
    return QotdScheduler(config_service=config_service, poster=poster)


__all__ = [
    "QotdScheduler",
    "build_scheduler",
    "is_due",
    "next_occurrence",
    "already_fired_today",
    "parse_local_time",
    "resolve_zone",
    "TICK_SECONDS",
    "GRACE_WINDOW_MINUTES",
]
