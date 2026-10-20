"""Host rotation reminder.

bot-hosting.net's free tier stops the bot unless the owner clicks "renew" in
their panel every 4 days. Nothing in the bot's environment knows when that
window started -- the timer lives entirely on the host's side -- so the only
clock available is the one the bot can observe for itself: **how long this
process has been running**.

That is the whole design:

  - start a marker file next to the log file recording the boot time
  - each tick, ask how many days since that recorded boot
  - once that reaches ``ROTATION_REMINDER_DAYS``, DM the bot owner with a single
    button (and record a ``bot_events`` row) so they can renew before the host
    stops the bot

Why uptime and not "days since last renewal": a renewal does not restart the
process, so on its own uptime drifts. But it is still the *safe* direction --
if the host restarts the bot for any reason (crash, platform reboot, a deploy),
the clock resets, which can only ever make the reminder *later*, never earlier.
Since the failure mode we care about is "the bot is silently gone", a reminder
that fires a little late on a freshly restarted process is acceptable, whereas
one that never fires is not.

Renewing now resets the clock without the owner having to remember a command:
the DM carries an **"I've renewed"** button, and pressing it restarts the
countdown. That button is a *persistent* view (fixed ``custom_id``,
``timeout=None``) because the message it lives on outlives the process -- the
click that matters most arrives right after the restart that a renewal causes,
so a view holding an in-memory reference would already be gone. Persistent views
are re-attached in ``setup_hook`` with ``add_view()``. ``/config dev renewed``
remains as the same operation from slash commands, sharing one code path with
the button.

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

# Persistent component id. Hardcoded (not random) because the view must survive a
# bot restart: the reminder DM lives in the owner's inbox forever, and after a
# renewal the bot restarts, which is exactly when the OLD message's button has to
# keep working. Persistent views are re-attached in setup_hook via add_view().
RENEW_BUTTON_ID = "candy:rotation:renewed"

# Where the owner actually renews. A Discord button cannot both navigate and run a
# callback, so the panel opens via a separate LINK button while "I've renewed"
# keeps resetting the countdown.
RENEW_PANEL_URL = "https://bot-hosting.net/a/billings"


def panel_button() -> discord.ui.Button:
    """The LINK button that opens the bot-hosting billing page.

    Kept as a factory so the reminder DM and every fallback (log channels, the
    acknowledged-message view) offer the same destination.
    """
    return discord.ui.Button(
        label="Open bot-hosting panel",
        style=discord.ButtonStyle.link,
        emoji="🔗",
        url=RENEW_PANEL_URL,
    )


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


class RenewalButtonView(discord.ui.View):
    """The single "I've renewed" button on the reminder DM.

    Persistent on purpose (``timeout=None`` + a fixed ``custom_id``). The
    reminder message sits in the owner's DMs indefinitely, and the click that
    matters most happens *after* a restart -- renewing the host restarts the bot,
    so a view holding an in-memory reference would be dead by then. A persistent
    view is re-attached at startup with ``bot.add_view()`` and routes clicks from
    messages sent by a previous process.

    The callback defers to the reminder's own ``mark_renewed`` so there is exactly
    one code path that resets the clock, shared with ``/config dev renewed``.
    """

    def __init__(self, reminder: RotationReminder | None = None) -> None:
        super().__init__(timeout=None)
        self._reminder = reminder
        # Rebind the id in case the class is instantiated before the module
        # constant is read; keeps a single source of truth for the id.
        self.children[0].custom_id = RENEW_BUTTON_ID  # type: ignore[union-attr]
        # The panel link rides on the same row. Appended here instead of declared
        # with @discord.ui.button: a LINK button has no custom_id and dispatches
        # no callback, and adding it in code keeps children[0] above stable.
        self.add_item(panel_button())

    @discord.ui.button(
        label="I've renewed",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id=RENEW_BUTTON_ID,
    )
    async def renewed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        reminder = self._reminder or getattr(interaction.client, "rotation_reminder", None)

        if reminder is None:
            await interaction.response.send_message(
                "The renewal reminder is disabled on this instance.", ephemeral=True
            )
            return

        if not await reminder.is_owner(interaction.user.id):
            await interaction.response.send_message(
                "Only the bot owner can acknowledge the renewal.", ephemeral=True
            )
            return

        moment = reminder.mark_renewed()

        # Edit the original message in place rather than sending a new one: the
        # button has served its purpose and must not be clicked twice by mistake.
        embed = reminder._build_embed(0.0, acknowledged_at=moment)
        embed.title = "✅ Renewal recorded"
        embed.colour = discord.Colour.green()

        view = discord.ui.View(timeout=None)
        disabled = discord.ui.Button(
            label="Renewed", style=discord.ButtonStyle.secondary, emoji="✅", disabled=True
        )
        view.add_item(disabled)
        # Keep the panel link alive on the acknowledged message too - the owner
        # may still need to reach the panel after resetting the countdown.
        view.add_item(panel_button())

        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.HTTPException as exc:
            log.warning("Could not edit the renewal reminder message: %s", exc)
            await interaction.response.send_message(
                f"Renewal recorded at {moment:%Y-%m-%d %H:%M} UTC.", ephemeral=True
            )

        log.info("Host renewal acknowledged by owner %s", interaction.user.id)
        await reminder.record_renewal_ack(interaction.user.id)


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
        # None = not yet resolved; a set (possibly empty) = resolved and cached.
        self._owner_ids: set[int] | None = None

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

    # --- ownership ---------------------------------------------------------

    async def is_owner(self, user_id: int) -> bool:
        """True when ``user_id`` owns the bot application.

        Used to gate the renewal button. Cached after the first successful
        resolution: a persistent view can be clicked at any time, and this must
        not hit the Discord API on every press. A failure is NOT cached, so a
        transient error does not lock the owner out permanently.
        """
        if self._owner_ids is not None:
            return user_id in self._owner_ids

        try:
            info = await self._bot.application_info()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not check bot ownership: %s", exc)
            return False

        if info.team is not None:
            ids = {m.id for m in info.team.members}
        elif info.owner is not None:
            ids = {info.owner.id}
        else:
            ids = set()

        self._owner_ids = ids
        return user_id in ids

    async def record_renewal_ack(self, user_id: int) -> None:
        """Record that the owner acknowledged a renewal.

        Account-level row (``guild_id`` NULL) so the dashboard feed shows that
        the reminder was acted on, not just that it fired.
        """
        payload = {
            "guild_id": None,
            "category": "config",
            "title": "Host renewal recorded",
            "description": "The bot owner acknowledged the renewal via the reminder DM.",
            "level": "info",
            "actor_id": user_id,
            "metadata": {"source": "rotation_reminder", "acknowledged": True},
        }
        try:
            await asyncio.to_thread(supabase_client.insert, "bot_events", payload)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not write renewal acknowledgement row: %s", exc)

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
        """Resolve the bot's owner for the DM.

        ``application_info().owner`` is a team owner when the app belongs to a
        team, in which case the individual members are the real recipients. The
        resolved ids are shared with ``is_owner`` via the cache.
        """
        try:
            info = await self._bot.application_info()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not fetch application info: %s", exc)
            return None

        if info.team is not None and info.team.members:
            self._owner_ids = {m.id for m in info.team.members}
            # Prefer the team owner, else the first member.
            for member in info.team.members:
                if member.id == getattr(info.team, "owner_id", None):
                    return member
            return info.team.members[0]

        if info.owner is not None:
            self._owner_ids = {info.owner.id}
        return info.owner

    async def _dm_owner(self, owner: discord.User, elapsed_days: float) -> bool:
        embed = self._build_embed(elapsed_days)
        view = RenewalButtonView(self)
        try:
            await owner.send(embed=embed, view=view)
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
                    embed=embed,
                    view=RenewalButtonView(self),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                posted = True
            except (discord.Forbidden, discord.NotFound):
                continue
            except discord.HTTPException as exc:
                log.warning("Renewal reminder post failed for guild %s: %s", guild_id, exc)

        if posted:
            await self._record_event(elapsed_days, delivered=True)
        return posted

    def _build_embed(
        self, elapsed_days: float, *, acknowledged_at: datetime | None = None
    ) -> discord.Embed:
        embed = discord.Embed(
            title="🔁 Host renewal due",
            description=(
                "bot-hosting.net's free tier stops this bot unless the plan is "
                "renewed **every 4 days**.\n\n"
                f"[Open the bot-hosting billing panel]({RENEW_PANEL_URL}) to renew, "
                "then press **I've renewed** below to restart the countdown."
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
        if acknowledged_at is not None:
            embed.add_field(
                name="Renewed at",
                value=f"<t:{int(acknowledged_at.timestamp())}:F>",
                inline=False,
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
    "RenewalButtonView",
    "build_reminder",
    "ensure_boot_time",
    "mark_renewed",
    "read_boot_time",
    "write_boot_time",
    "days_elapsed",
    "is_due",
    "TICK_SECONDS",
    "RESEND_EVERY",
    "RENEW_BUTTON_ID",
    "RENEW_PANEL_URL",
    "panel_button",
]
