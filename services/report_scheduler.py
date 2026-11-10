"""Monthly report scheduler.

The report covers the PREVIOUS calendar month, in the guild's own timezone.
That is the useful definition: a report generated on 1 November covers 1-31
October, and the period is closed, so its numbers never change afterwards.

Time is handled the same way as QOTD (services/qotd_scheduler.py): the schedule
is a local day-of-month + local time + IANA zone, recomputed from local time on
every tick. DST is therefore a wall-clock shift, not a UTC offset shift.

Restart safety
    ``report_last_generated_period`` stores the period_start of the last
    automatic report. A restart near the scheduled minute cannot double-post,
    and a bot that was down on the 1st still generates when it comes back - the
    scheduler looks for the most recent scheduled moment, not "today".

Missed months
    Only the most recent scheduled period is generated. If the bot was offline
    for several months, older periods are logged as skipped rather than
    flooding the channel with a backlog of reports when it returns.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Awaitable, Callable

import discord

from db import config_cache
from db.config_cache import GuildConfig
from services.config_service import ConfigService
from services.report_service import ReportPeriod, resolve_period
from utils.logging_setup import get_logger
from utils.timezones import resolve_zone

log = get_logger("services.report_scheduler")

TICK_SECONDS = 300.0
# A failed generation (missing channel, Discord outage) is retried, but not on
# every tick: an admin has time to fix permissions without a log flood.
RETRY_SECONDS = 1800.0


def parse_report_time(raw: str | None) -> dtime | None:
    """Parse a Postgres ``time`` value such as '09:00:00' into a dtime."""
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%H:%M:%S", "%H:%M", "%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    log.warning("Could not parse report_time %r", raw)
    return None


def latest_scheduled_moment(
    now_local: datetime, day: int, local_time: dtime
) -> datetime:
    """The most recent scheduled moment at or before ``now_local``.

    ``day`` is clamped to 1..28 (the schema constraint) so February can never
    silently skip a report.
    """
    day = max(1, min(28, int(day)))
    zone = now_local.tzinfo
    candidate = datetime.combine(
        date(now_local.year, now_local.month, day), local_time
    ).replace(tzinfo=zone)
    if candidate <= now_local:
        return candidate

    # Not reached yet this month: use the same day last month.
    previous_month_end = date(now_local.year, now_local.month, 1) - timedelta(days=1)
    return datetime.combine(
        date(previous_month_end.year, previous_month_end.month, day), local_time
    ).replace(tzinfo=zone)


def due_period(config: GuildConfig, now_utc: datetime) -> ReportPeriod | None:
    """Return the period that should be generated now, or None.

    Pure and side-effect free, so tests can drive it with a fixed clock.
    """
    if not config.report_enabled:
        return None
    if config.report_channel_id is None:
        return None

    local_time = parse_report_time(config.report_time)
    if local_time is None:
        return None

    zone = resolve_zone(config.report_timezone)
    now_local = now_utc.astimezone(zone)
    scheduled = latest_scheduled_moment(now_local, config.report_day, local_time)

    # The scheduled moment's month is the month the report is DELIVERED in; the
    # period it covers is the month before it.
    year, month = scheduled.year, scheduled.month - 1
    if month == 0:
        year -= 1
        month = 12

    period = resolve_period(zone, year=year, month=month)
    if config.report_last_generated_period == period.start.isoformat():
        return None
    return period


def _months_between(earlier: date, later: date) -> int:
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


class ReportScheduler:
    """Polls each guild's report schedule and asks the cog to generate one."""

    def __init__(
        self,
        *,
        config_service: ConfigService,
        runner: Callable[[discord.Guild, GuildConfig, ReportPeriod], Awaitable[bool]],
        get_guild: Callable[[int], discord.Guild | None],
    ) -> None:
        self._config = config_service
        self._runner = runner
        self._get_guild = get_guild
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_tick: datetime | None = None
        self._attempts: dict[tuple[int, str], float] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="report-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        log.info("Report scheduler stopped")

    @property
    def last_tick(self) -> datetime | None:
        return self._last_tick

    # -- loop --------------------------------------------------------------

    async def _run(self) -> None:
        await asyncio.sleep(30)  # let startup settle
        log.info(
            "Report scheduler started (tick every %.0fs, local-day schedule)",
            TICK_SECONDS,
        )

        while not self._stopping.is_set():
            started = asyncio.get_event_loop().time()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("Report tick failed: %s", exc, exc_info=True)

            elapsed = asyncio.get_event_loop().time() - started
            delay = max(10.0, TICK_SECONDS - elapsed)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    async def tick(self, *, now_utc: datetime | None = None) -> None:
        """One scheduling pass. Also used by tests."""
        now = now_utc or datetime.now(timezone.utc)
        self._last_tick = now
        loop_time = asyncio.get_event_loop().time()

        for guild_id in config_cache.all_guild_ids():
            config = config_cache.get(guild_id)
            try:
                period = due_period(config, now)
            except Exception as exc:  # noqa: BLE001 - a bad row must not stop others
                log.warning("Could not evaluate report schedule for %s: %s", guild_id, exc)
                continue

            if period is None:
                continue

            key = (guild_id, period.start.isoformat())
            last_attempt = self._attempts.get(key)
            if last_attempt is not None and (loop_time - last_attempt) < RETRY_SECONDS:
                continue

            guild = self._get_guild(guild_id)
            if guild is None:
                continue

            self._log_skipped(config, period)

            self._attempts[key] = loop_time
            try:
                delivered = await self._runner(guild, config, period)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "Automatic report generation failed for guild %s (%s): %s",
                    guild_id,
                    period.label,
                    exc,
                    exc_info=True,
                )
                continue

            if not delivered:
                log.warning(
                    "Automatic report for guild %s (%s) was not delivered; retrying later",
                    guild_id,
                    period.label,
                )
                continue

            self._attempts.pop(key, None)
            try:
                await asyncio.to_thread(
                    self._config.set_report_last_generated_period,
                    guild_id,
                    period.start,
                )
            except Exception as exc:  # noqa: BLE001
                # The report is already posted. A failed stamp only risks a
                # duplicate next tick, which is better than losing the report.
                log.error(
                    "Report posted for guild %s but the period stamp failed: %s",
                    guild_id,
                    exc,
                )

    @staticmethod
    def _log_skipped(config: GuildConfig, period: ReportPeriod) -> None:
        last = config.report_last_generated_period
        if not last:
            return
        try:
            last_start = date.fromisoformat(str(last)[:10])
        except ValueError:
            return
        missing = _months_between(last_start, period.start) - 1
        if missing > 0:
            log.warning(
                "Guild %s: %d report period(s) were missed before %s; "
                "only the most recent one will be generated",
                config.guild_id,
                missing,
                period.label,
            )


__all__ = [
    "ReportScheduler",
    "due_period",
    "latest_scheduled_moment",
    "parse_report_time",
    "TICK_SECONDS",
]
