"""Host rotation reminder.

bot-hosting.net's free tier stops the bot unless the owner clicks "renew" in
their panel every 4 days. Nothing in the bot's environment knows when that
window started -- the timer lives entirely on the host's side -- so the only
clock available is the one the bot can observe for itself: **how long this
process has been running**.

That is the whole design:

  - start a marker file next to the log file recording the boot time
  - each tick, ask how many days since that recorded boot
  - once that reaches ``ROTATION_REMINDER_DAYS``, DM the bot owner (and record a
    ``bot_events`` row) so they can renew before the host stops the bot

Why uptime and not "days since last renewal": a renewal does not restart the
process, so on its own uptime drifts. But it is still the *safe* direction --
if the host restarts the bot for any reason (crash, platform reboot, a deploy),
the clock resets, which can only ever make the reminder *later*, never earlier.
Since the failure mode we care about is "the bot is silently gone", a reminder
that fires a little late on a freshly restarted process is acceptable, whereas
one that never fires is not.

The marker file, not ``time.monotonic()``: uptime that resets on a graceful
restart is still a restart, and the marker survives the process so the actual
boot wall-clock time is used. It is best-effort -- if the path is unwritable the
reminder degrades to "days since this process started", which is the same thing
in the common case.

DM first, log channel second: the owner's DM is private and needs no per-guild
config. If DMs are closed (Forbidden) the reminder falls back to each guild's
configured log channel so it is not silently lost, and always writes a
``bot_events`` row for the dashboard feed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord

from db import config_cache, supabase_client
from utils.logging_setup import get_logger

log = get_logger("services.rotation")

# How often to re-evaluate the clock. The reminder is a days-scale signal, so a
# coarse tick is plenty; hourly keeps it cheap and near-instant after a restart.
TICK_SECONDS = 60 * 60.0

# Re-send the reminder at most once per this window, so a long-running process
# past the threshold does not DM every hour.
RESEND_EVERY = timedelta(hours=12)

# Marker file lives beside the log file so it lands in a writable directory that
# already exists on the host (logs/ is in .gitignore).
MARKER_NAME = ".boot_time"


def _parse_iso(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_boot_time(marker_path: Path) -> datetime | None:
    """Read the recorded boot time, or None when absent/unreadable."""
    try:
        if not marker_path.exists():
            return None
        return _parse_iso(marker_path.read_text(encoding="utf-8"))
    except OSError as exc:
        log.debug("Could not read boot marker %s: %s", marker_path, exc)
        return None


def write_boot_time(marker_path: Path, when: datetime) -> bool:
    """Record a boot time. Best-effort; returns whether it was written."""
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(when.isoformat(), encoding="utf-8")
        return True
    except OSError as exc:
        log.debug("Could not write boot marker %s: %s", marker_path, exc)
        return False


def ensure_boot_time(marker_path: Path, now: datetime) -> datetime:
    """Return the recorded boot time, writing ``now`` when none exists.

    Deliberately does NOT overwrite an existing marker: every restart would then
    reset the clock, and a host that restarts hourly would never reach the
    threshold. Only a graceful renewal (``mark_renewed``) should reset it.
    """
    existing = read_boot_time(marker_path)
    if existing is not None:
        return existing
    write_boot_time(marker_path, now)
    return now


def mark_renewed(marker_path: Path, now: datetime | None = None) -> datetime:
    """Reset the clock to now. Called by /config dev renewed."""
    moment = now or datetime.now(timezone.utc)
    write_boot_time(marker_path, moment)
    return moment


def days_elapsed(boot_time: datetime, now: datetime) -> float:
    """Whole days since ``boot_time``, floored at zero."""
    delta = now - boot_time
    return max(0.0, delta.total_seconds() / 86400.0)


def is_due(boot_time: datetime, now: datetime, threshold_days: float) -> bool:
    """True when the reminder threshold has been reached."""
    if threshold_days <= 0:
        return False
    return days_elapsed(boot_time, now) >= threshold_days


class RotationReminder:
    """Periodically reminds the bot owner to renew the host.

    Not per-guild: the host renewal is an account-level concern, so this runs
    once regardless of how many guilds the bot is in.
    """

    def __init__(
        self,
        *,
        bot: Any,
        marker_path: Path,
        threshold_days: float,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self._bot = bot
        self._marker_path = marker_path
        self._threshold_days = threshold_days
        self._tick_seconds = tick_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

        self._boot_time: datetime | None = None
        self._last_sent_at: datetime | None = None
        self._last_tick: datetime | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="rotation-reminder")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        log.info("Rotation reminder stopped")

    # --- clock -------------------------------------------------------------

    @property
    def boot_time(self) -> datetime | None:
        return self._boot_time

    @property
    def threshold_days(self) -> float:
        return self._threshold_days

    @property
    def last_tick(self) -> datetime | None:
        return self._last_tick

    def mark_renewed(self, now: datetime | None = None) -> datetime:
        """Externally reset the clock (e.g. after a manual renewal)."""
        moment = mark_renewed(self._marker_path, now)
        self._boot_time = moment
        self._last_sent_at = None
        log.info("Rotation clock reset; next reminder in %.1f day(s)", self._threshold_days)
        return moment

    # --- loop --------------------------------------------------------------

    async def _run(self) -> None:
        now = datetime.now(timezone.utc)
        self._boot_time = ensure_boot_time(self._marker_path, now)

        if self._threshold_days <= 0:
            log.info("Rotation reminder disabled (ROTATION_REMINDER_DAYS=0)")
            return

        log.info(
            "Rotation reminder started: threshold %.1f day(s), booted %s UTC (%s)",
            self._threshold_days,
            self._boot_time.isoformat(),
            self._marker_path,
        )

        # Evaluate once immediately so a process restarted past the threshold
        # (the exact case that matters: the host restarted, the clock is fresh,
        # but the reminder surfaces on the first check rather than an hour in)
        # still reports its state.
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                log.error("Rotation reminder tick failed: %s", exc, exc_info=True)

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._tick_seconds)

    async def tick(self, *, now: datetime | None = None) -> bool:
        """One evaluation. Returns True when a reminder was delivered.

        Separated from the loop so tests can drive it without sleeping.
        """
        moment = now or datetime.now(timezone.utc)
        self._last_tick = moment

        if self._boot_time is None:
            self._boot_time = ensure_boot_time(self._marker_path, moment)

        if not is_due(self._boot_time, moment, self._threshold_days):
            return False

        if self._last_sent_at is not None and moment - self._last_sent_at < RESEND_EVERY:
            return False

        elapsed = days_elapsed(self._boot_time, moment)
        log.warning(
            "Host renewal due: %.1f day(s) since boot (threshold %.1f)",
            elapsed,
            self._threshold_days,
        )

        delivered = await self._deliver(elapsed)
        if delivered:
            self._last_sent_at = moment
        return delivered

    # --- delivery ----------------------------------------------------------

    async def _deliver(self, elapsed_days: float) -> bool:
        owner = await self._owner()

        if owner is not None:
            if await self._dm_owner(owner, elapsed_days):
                return True

        # DM failed or no owner resolved: fall back to every guild's log channel.
        posted = await self._post_to_log_channels(elapsed_days)
        if posted:
            return True

        # Last resort: the dashboard feed. Not delivered, so the retry window
        # stays open and the next tick tries again.
        await self._record_event(elapsed_days, delivered=False)
        return False

    async def _owner(self) -> discord.User | None:
        """Resolve the bot's owner.

        ``application_info().owner`` is a team owner when the app belongs to a
        team, in which case the individual members are the real recipients.
        """
        try:
            info = await self._bot.application_info()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not fetch application info: %s", exc)
            return None

        if info.team is not None and info.team.members:
            # Prefer the team owner, else the first member.
            for member in info.team.members:
                if member.id == getattr(info.team, "owner_id", None):
                    return member
            return info.team.members[0]

        return info.owner

    async def _dm_owner(self, owner: discord.User, elapsed_days: float) -> bool:
        embed = self._build_embed(elapsed_days)
        try:
            await owner.send(embed=embed)
        except discord.Forbidden:
            log.warning("Owner DMs are closed; falling back to log channels")
            return False
        except discord.HTTPException as exc:
            log.warning("Owner DM failed: %s", exc)
            return False

        log.info("Sent renewal reminder to owner %s", owner.id)
        await self._record_event(elapsed_days, delivered=True)
        return True

    async def _post_to_log_channels(self, elapsed_days: float) -> bool:
        posted = False
        embed = self._build_embed(elapsed_days)

        for guild_id in config_cache.all_guild_ids():
            try:
                config = config_cache.get(guild_id)
            except Exception:  # noqa: BLE001
                continue
            if not config.log_enabled or config.log_channel_id is None:
                continue

            guild = self._bot.get_guild(guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(config.log_channel_id)
            if channel is None or not isinstance(channel, discord.abc.Messageable):
                continue

            try:
                await channel.send(
                    embed=embed, allowed_mentions=discord.AllowedMentions.none()
                )
                posted = True
            except (discord.Forbidden, discord.NotFound):
                continue
            except discord.HTTPException as exc:
                log.warning("Renewal reminder post failed for guild %s: %s", guild_id, exc)

        if posted:
            await self._record_event(elapsed_days, delivered=True)
        return posted

    def _build_embed(self, elapsed_days: float) -> discord.Embed:
        embed = discord.Embed(
            title="🔁 Host renewal due",
            description=(
                "bot-hosting.net's free tier stops this bot unless the plan is "
                "renewed **every 4 days**.\n\n"
                "Renew in the bot-hosting panel to avoid downtime. Once you have "
                "renewed, run `/config dev renewed` to reset this reminder."
            ),
            colour=discord.Colour.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Running for",
            value=f"{elapsed_days:.1f} day(s)",
            inline=True,
        )
        embed.add_field(
            name="Reminder set at",
            value=f"{self._threshold_days:.1f} day(s)",
            inline=True,
        )
        embed.set_footer(text="candy_v2 · host rotation")
        return embed

    async def _record_event(self, elapsed_days: float, *, delivered: bool) -> None:
        """Write an account-level row to bot_events.

        ``guild_id`` is nullable in the schema, so the reminder is recorded even
        when the bot is in no guilds. Wrapped in its own try/except: the feed is
        nice-to-have and must never break the reminder.
        """
        payload = {
            "guild_id": None,
            "category": "errors",
            "title": "Host renewal due",
            "description": (
                f"The bot has been running for {elapsed_days:.1f} day(s). "
                "bot-hosting.net requires a manual renewal every 4 days. "
                + ("Reminder delivered." if delivered else "Reminder NOT delivered.")
            ),
            "level": "warning",
            "metadata": {
                "source": "rotation_reminder",
                "elapsed_days": round(elapsed_days, 2),
                "threshold_days": self._threshold_days,
                "delivered": delivered,
            },
        }
        try:
            await asyncio.to_thread(supabase_client.insert, "bot_events", payload)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not write rotation reminder row: %s", exc)


def build_reminder(*, bot: Any, log_file: str, threshold_days: float) -> RotationReminder:
    """Construct a reminder whose marker sits beside the log file."""
    marker = Path(os.path.dirname(log_file) or ".") / MARKER_NAME
    return RotationReminder(
        bot=bot, marker_path=marker, threshold_days=threshold_days
    )


__all__ = [
    "RotationReminder",
    "build_reminder",
    "ensure_boot_time",
    "mark_renewed",
    "read_boot_time",
    "write_boot_time",
    "days_elapsed",
    "is_due",
    "TICK_SECONDS",
    "RESEND_EVERY",
]
