"""In-memory activity counters for the monthly report.

Why memory and not a write per message
    The bot runs on a 256 MB host. A database round-trip for every message
    would be both slow and expensive. Instead every counter lives in a plain
    dict and is flushed every few minutes with ONE RPC per guild
    (``activity_flush``). A crash loses at most one flush window; that is the
    documented trade.

What is counted
    - messages (bots excluded)
    - voice seconds and voice joins (bots excluded, AFK channel included)
    - joins and leaves
    - boosts gained/lost
    - per local day, hour, channel and member

No message content is stored anywhere. Per-member rows carry a display-name
snapshot so a report still reads correctly after someone leaves; the report
service refreshes those names from Discord at render time.

Voice sessions
    A connected member is a *session* (channel + last-accounted monotonic
    time). Each flush accounts the elapsed seconds into the current local
    day/hour and resets the clock, so a long call is spread across flushes
    instead of being credited to one bucket when it ends. Sessions are seeded
    from ``guild.voice_states`` on startup, so a restart does not lose the
    people who were already in voice.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping

import discord

from db import supabase_client
from services.config_service import ConfigService
from utils.logging_setup import get_logger
from utils.timezones import resolve_zone

log = get_logger("services.activity")

# A suspended host can wake up with a huge monotonic gap. Never credit more
# than an hour of "bot online" time to one flush.
MAX_ONLINE_DELTA_SECONDS = 3600


# ---------------------------------------------------------------------------
# buckets
# ---------------------------------------------------------------------------


@dataclass
class _ChannelBucket:
    name: str | None = None
    messages: int = 0
    voice_seconds: int = 0
    voice_joins: int = 0


@dataclass
class _MemberBucket:
    name: str | None = None
    messages: int = 0
    voice_seconds: int = 0
    voice_joins: int = 0


@dataclass
class _HourBucket:
    messages: int = 0
    voice_seconds: int = 0


@dataclass
class _DayBucket:
    day: date
    joins: int = 0
    leaves: int = 0
    boosts_gained: int = 0
    boosts_lost: int = 0
    messages: int = 0
    voice_seconds: int = 0
    voice_joins: int = 0
    bot_online_seconds: int = 0
    channels: dict[int, _ChannelBucket] = field(default_factory=dict)
    hours: dict[int, _HourBucket] = field(default_factory=dict)
    members: dict[int, _MemberBucket] = field(default_factory=dict)

    def channel(self, channel_id: int, name: str | None) -> _ChannelBucket:
        bucket = self.channels.get(channel_id)
        if bucket is None:
            bucket = _ChannelBucket(name=name)
            self.channels[channel_id] = bucket
        elif name:
            bucket.name = name
        return bucket

    def hour(self, hour: int) -> _HourBucket:
        bucket = self.hours.get(hour)
        if bucket is None:
            bucket = _HourBucket()
            self.hours[hour] = bucket
        return bucket

    def member(self, user_id: int, name: str | None) -> _MemberBucket:
        bucket = self.members.get(user_id)
        if bucket is None:
            bucket = _MemberBucket(name=name)
            self.members[user_id] = bucket
        elif name:
            bucket.name = name
        return bucket

    def merge(self, other: "_DayBucket") -> None:
        """Add another bucket into this one (used when a flush fails)."""
        self.joins += other.joins
        self.leaves += other.leaves
        self.boosts_gained += other.boosts_gained
        self.boosts_lost += other.boosts_lost
        self.messages += other.messages
        self.voice_seconds += other.voice_seconds
        self.voice_joins += other.voice_joins
        self.bot_online_seconds += other.bot_online_seconds

        for channel_id, source in other.channels.items():
            target = self.channel(channel_id, source.name)
            target.messages += source.messages
            target.voice_seconds += source.voice_seconds
            target.voice_joins += source.voice_joins

        for hour, source in other.hours.items():
            target_hour = self.hour(hour)
            target_hour.messages += source.messages
            target_hour.voice_seconds += source.voice_seconds

        for user_id, source in other.members.items():
            target_member = self.member(user_id, source.name)
            target_member.messages += source.messages
            target_member.voice_seconds += source.voice_seconds
            target_member.voice_joins += source.voice_joins

    @property
    def empty(self) -> bool:
        return not (
            self.joins
            or self.leaves
            or self.boosts_gained
            or self.boosts_lost
            or self.messages
            or self.voice_seconds
            or self.voice_joins
            or self.bot_online_seconds
            or self.channels
            or self.hours
            or self.members
        )


@dataclass
class _VoiceSession:
    channel_id: int
    channel_name: str | None
    last_accounted: float


# ---------------------------------------------------------------------------
# tracker
# ---------------------------------------------------------------------------


class ActivityTracker:
    """Thread-safe in-memory counters with one RPC per guild per flush."""

    def __init__(
        self,
        config_service: ConfigService,
        *,
        retention_days: int = 400,
    ) -> None:
        self._config = config_service
        self._retention_days = max(30, int(retention_days))
        self._lock = threading.RLock()
        self._days: dict[int, dict[date, _DayBucket]] = {}
        self._voice: dict[tuple[int, int], _VoiceSession] = {}
        self._pending_boosts: dict[int, list[dict[str, Any]]] = {}
        self._last_flush: dict[int, float] = {}
        self._last_prune: float = 0.0

    # -- time helpers ------------------------------------------------------

    def _zone(self, guild_id: int):
        try:
            return resolve_zone(self._config.get(guild_id).report_timezone)
        except Exception:  # noqa: BLE001 - a bad row must not break counting
            return timezone.utc

    def _local_now(self, guild_id: int) -> datetime:
        return datetime.now(timezone.utc).astimezone(self._zone(guild_id))

    def _bucket(self, guild_id: int, day: date) -> _DayBucket:
        per_day = self._days.setdefault(guild_id, {})
        bucket = per_day.get(day)
        if bucket is None:
            bucket = _DayBucket(day=day)
            per_day[day] = bucket
        return bucket

    # -- counters ----------------------------------------------------------

    def record_message(
        self,
        guild_id: int,
        *,
        channel_id: int | None,
        channel_name: str | None,
        user_id: int,
        user_name: str | None,
        is_bot: bool = False,
    ) -> None:
        if is_bot:
            return
        local = self._local_now(guild_id)
        with self._lock:
            bucket = self._bucket(guild_id, local.date())
            bucket.messages += 1
            bucket.hour(local.hour).messages += 1
            bucket.member(user_id, user_name).messages += 1
            if channel_id is not None:
                bucket.channel(channel_id, channel_name).messages += 1

    def record_join(
        self, guild_id: int, *, user_id: int, user_name: str | None, is_bot: bool = False
    ) -> None:
        if is_bot:
            return
        local = self._local_now(guild_id)
        with self._lock:
            self._bucket(guild_id, local.date()).joins += 1

    def record_leave(
        self, guild_id: int, *, user_id: int, user_name: str | None, is_bot: bool = False
    ) -> None:
        if is_bot:
            return
        local = self._local_now(guild_id)
        with self._lock:
            self._bucket(guild_id, local.date()).leaves += 1

    def record_boost(
        self,
        guild_id: int,
        *,
        user_id: int | None,
        user_name: str | None,
        kind: str,
        occurred_at: datetime | None = None,
    ) -> None:
        """Record one boost start/stop.

        The event row is queued and inserted on the next flush. Boosts are rare
        enough that this could be a direct insert, but keeping one write path
        means a failed flush retries instead of losing the event.
        """
        if kind not in {"start", "stop"}:
            raise ValueError(f"invalid boost kind: {kind!r}")

        local = self._local_now(guild_id)
        moment = occurred_at or datetime.now(timezone.utc)
        with self._lock:
            bucket = self._bucket(guild_id, local.date())
            if kind == "start":
                bucket.boosts_gained += 1
            else:
                bucket.boosts_lost += 1

            self._pending_boosts.setdefault(guild_id, []).append(
                {
                    "guild_id": guild_id,
                    "occurred_at": moment.isoformat(),
                    "user_id": user_id,
                    "user_name": user_name,
                    "kind": kind,
                    "source": "gateway",
                }
            )

    # -- voice -------------------------------------------------------------

    def update_voice_state(
        self,
        guild_id: int,
        *,
        user_id: int,
        user_name: str | None,
        before_channel: Any,
        after_channel: Any,
        is_bot: bool = False,
    ) -> None:
        """Handle one voice-state transition.

        ``before_channel``/``after_channel`` are discord channel objects or
        None. A mute/deafen change (same channel) is ignored.
        """
        if is_bot:
            return

        before_id = getattr(before_channel, "id", None)
        after_id = getattr(after_channel, "id", None)
        if before_id == after_id:
            return

        now = time.monotonic()
        key = (guild_id, user_id)
        with self._lock:
            if before_id is not None:
                self._account_voice_locked(guild_id, key, now, user_name=user_name)

            if after_id is None:
                self._voice.pop(key, None)
                return

            self._voice[key] = _VoiceSession(
                channel_id=after_id,
                channel_name=getattr(after_channel, "name", None),
                last_accounted=now,
            )

            # A "join" is entering voice from outside, not switching channels.
            if before_id is None:
                local = self._local_now(guild_id)
                bucket = self._bucket(guild_id, local.date())
                bucket.voice_joins += 1
                bucket.member(user_id, user_name).voice_joins += 1
                bucket.channel(after_id, getattr(after_channel, "name", None)).voice_joins += 1

    def seed_voice_states(
        self, guild_id: int, voice_states: Mapping[int, discord.VoiceState]
    ) -> int:
        """Start sessions for members already connected when the bot boots.

        Returns the number of sessions seeded. Existing sessions are left
        alone, so calling this twice is safe.
        """
        now = time.monotonic()
        seeded = 0
        with self._lock:
            for user_id, state in voice_states.items():
                channel = getattr(state, "channel", None)
                if channel is None:
                    continue
                member = getattr(state, "member", None)
                if member is not None and getattr(member, "bot", False):
                    continue
                key = (guild_id, user_id)
                if key in self._voice:
                    continue
                self._voice[key] = _VoiceSession(
                    channel_id=channel.id,
                    channel_name=getattr(channel, "name", None),
                    last_accounted=now,
                )
                seeded += 1
        if seeded:
            log.info("Seeded %d active voice session(s) for guild %s", seeded, guild_id)
        return seeded

    def _account_voice_locked(
        self,
        guild_id: int,
        key: tuple[int, int],
        now: float,
        *,
        user_name: str | None = None,
    ) -> None:
        session = self._voice.get(key)
        if session is None:
            return

        elapsed = int(now - session.last_accounted)
        if elapsed <= 0:
            return

        local = self._local_now(guild_id)
        bucket = self._bucket(guild_id, local.date())
        bucket.voice_seconds += elapsed
        bucket.hour(local.hour).voice_seconds += elapsed
        bucket.channel(session.channel_id, session.channel_name).voice_seconds += elapsed
        bucket.member(key[1], user_name).voice_seconds += elapsed
        session.last_accounted = now

    def _account_guild_voice_locked(self, guild_id: int, now: float) -> None:
        for key in [k for k in self._voice if k[0] == guild_id]:
            self._account_voice_locked(guild_id, key, now)

    # -- flushing ----------------------------------------------------------

    def flush_guild(
        self,
        guild: discord.Guild,
        *,
        online_count: int | None = None,
    ) -> int:
        """Flush one guild's counters. Blocking; call via ``asyncio.to_thread``.

        Returns the number of day buckets written. Failed buckets are merged
        back into memory so the next flush retries them.
        """
        guild_id = guild.id
        now = time.monotonic()
        local = self._local_now(guild_id)

        with self._lock:
            self._account_guild_voice_locked(guild_id, now)

            last = self._last_flush.get(guild_id)
            if last is not None:
                delta = min(MAX_ONLINE_DELTA_SECONDS, max(0, int(now - last)))
                if delta:
                    self._bucket(guild_id, local.date()).bot_online_seconds += delta
            self._last_flush[guild_id] = now

            buckets = self._days.pop(guild_id, {})
            boosts = self._pending_boosts.pop(guild_id, [])

        written = 0
        failed: list[_DayBucket] = []

        for bucket in buckets.values():
            if bucket.empty:
                continue
            payload = self._payload(guild, bucket, online_count=online_count)
            try:
                supabase_client.call_rpc(
                    "activity_flush", {"p_guild_id": guild_id, "p_payload": payload}
                )
                written += 1
            except Exception as exc:  # noqa: BLE001 - retry on the next flush
                log.warning(
                    "Activity flush failed for guild %s day %s: %s",
                    guild_id,
                    bucket.day,
                    exc,
                )
                failed.append(bucket)

        if boosts:
            try:
                supabase_client.insert("guild_boost_events", boosts)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not record boost events for guild %s: %s", guild_id, exc)
                with self._lock:
                    self._pending_boosts.setdefault(guild_id, []).extend(boosts)

        if failed:
            with self._lock:
                for bucket in failed:
                    self._bucket(guild_id, bucket.day).merge(bucket)

        return written

    def _payload(
        self,
        guild: discord.Guild,
        bucket: _DayBucket,
        *,
        online_count: int | None,
    ) -> dict[str, Any]:
        channels = [
            {
                "channel_id": channel_id,
                "channel_name": item.name or "",
                "messages": item.messages,
                "voice_seconds": item.voice_seconds,
                "voice_joins": item.voice_joins,
            }
            for channel_id, item in bucket.channels.items()
            if item.messages or item.voice_seconds or item.voice_joins
        ]
        hours = [
            {
                "hour": hour,
                "messages": item.messages,
                "voice_seconds": item.voice_seconds,
            }
            for hour, item in bucket.hours.items()
            if item.messages or item.voice_seconds
        ]
        members = [
            {
                "user_id": user_id,
                "user_name": item.name or "",
                "messages": item.messages,
                "voice_seconds": item.voice_seconds,
                "voice_joins": item.voice_joins,
            }
            for user_id, item in bucket.members.items()
            if item.messages or item.voice_seconds or item.voice_joins
        ]

        return {
            "day": bucket.day.isoformat(),
            "member_count": guild.member_count,
            "online_count": online_count,
            "boost_count": guild.premium_subscription_count,
            "daily": {
                "joins": bucket.joins,
                "leaves": bucket.leaves,
                "boosts_gained": bucket.boosts_gained,
                "boosts_lost": bucket.boosts_lost,
                "messages": bucket.messages,
                "voice_seconds": bucket.voice_seconds,
                "voice_joins": bucket.voice_joins,
                "bot_online_seconds": bucket.bot_online_seconds,
            },
            "channels": channels,
            "hours": hours,
            "members": members,
        }

    def prune(self, *, force: bool = False) -> int:
        """Delete per-member/channel/hour rows older than the retention window.

        Throttled to once per 24h unless ``force`` is true. Blocking.
        """
        now = time.monotonic()
        if not force and (now - self._last_prune) < 86400:
            return 0
        try:
            removed = int(
                supabase_client.call_rpc(
                    "activity_prune", {"p_days": self._retention_days}
                )
                or 0
            )
            self._last_prune = now
            if removed:
                log.info(
                    "Pruned %d activity row(s) older than %d days",
                    removed,
                    self._retention_days,
                )
            return removed
        except Exception as exc:  # noqa: BLE001
            log.warning("Activity prune failed: %s", exc)
            return 0

    # -- introspection -----------------------------------------------------

    def pending(self, guild_id: int) -> dict[str, int]:
        """In-memory totals not yet flushed. Used by tests and status output."""
        with self._lock:
            buckets = self._days.get(guild_id, {})
            return {
                "days": len(buckets),
                "messages": sum(b.messages for b in buckets.values()),
                "voice_seconds": sum(b.voice_seconds for b in buckets.values()),
                "voice_sessions": sum(1 for k in self._voice if k[0] == guild_id),
                "boosts": len(self._pending_boosts.get(guild_id, [])),
            }

    def clear(self) -> None:
        """Drop all in-memory state. Tests only."""
        with self._lock:
            self._days.clear()
            self._voice.clear()
            self._pending_boosts.clear()
            self._last_flush.clear()


__all__ = ["ActivityTracker"]
