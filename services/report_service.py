"""Monthly server report: data collection, narrative storage, and rendering.

The report has exactly seven sections (see :data:`NARRATIVE_FIELDS` and
:func:`render_report`). Numbers come from ``report_summary()`` in Postgres; the
human-written parts live in ``guild_reports.narrative`` and are preserved when
the report is regenerated.

Why the aggregate lives in SQL
    A month of per-member rows is small, but pulling it to a 256 MB bot host
    just to sum it is wasteful. ``report_summary`` returns one JSON object with
    every number the template needs.

Why the report stores a frozen copy
    ``auto_data`` is the summary JSON from the moment of generation. Retention
    prunes per-member/channel/hour detail after ~400 days, but a published
    report must keep reading correctly. The markdown is stored too, so viewing
    an old report does not depend on the source rows still existing.

Narrative fields
    ``notable_contributions``, ``community_achievements``, ``positive_feedback``,
    ``staff_issues``, ``problems_faced``, ``complaints``, ``suggestions``,
    ``actions_taken``, ``improvements_needed``, ``final_summary``,
    ``personal_opinions``, ``extra``.
"""

from __future__ import annotations

import asyncio
import calendar
import io
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Any

import discord

from db import supabase_client
from services.config_service import ConfigService
from services.ticket_log import TICKET_TYPE_ORDER
from utils.logging_setup import get_logger
from utils.timezones import resolve_zone

log = get_logger("services.report")

# Human-written sections, in report order. The key is what is stored in
# guild_reports.narrative; the value is the heading shown in the report.
NARRATIVE_FIELDS: dict[str, str] = {
    "notable_contributions": "Notable Contributions",
    "community_achievements": "Community Achievements",
    "positive_feedback": "Positive Feedback",
    "staff_issues": "Staff Issues",
    "problems_faced": "Problems Faced",
    "complaints": "Complaints",
    "suggestions": "Suggestions",
    "actions_taken": "Actions Taken",
    "improvements_needed": "Improvements Needed",
    "final_summary": "Final Summary",
    "personal_opinions": "Personal opinions",
    "extra": "Extra",
}

NOT_PROVIDED = "_(not provided)_"


class ReportError(ValueError):
    """A report could not be generated, stored, or delivered."""


@dataclass
class PublishResult:
    """Outcome of :meth:`ReportService.publish`.

    ``edited`` is true when an already-published message was updated in place
    instead of a second copy being posted.
    """

    row: dict[str, Any]
    edited: bool = False


@dataclass(frozen=True)
class ReportPeriod:
    """One calendar month, with its UTC boundaries when known."""

    start: date
    end: date
    start_at: datetime | None = None
    end_at: datetime | None = None

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def label(self) -> str:
        return f"{self.start:%B %Y}"


# ---------------------------------------------------------------------------
# period resolution
# ---------------------------------------------------------------------------


def resolve_period(
    zone: tzinfo,
    *,
    now: datetime | None = None,
    year: int | None = None,
    month: int | None = None,
) -> ReportPeriod:
    """Resolve a report period in the guild's own timezone.

    With no year/month, this is the PREVIOUS calendar month: a report generated
    on 1 November covers 1-31 October. That is what "monthly report" means to
    an admin and it keeps the period closed, so the numbers never change after
    the report is posted.
    """
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)

    if year is not None or month is not None:
        if year is None or month is None:
            raise ReportError("year and month must be provided together")
        if not (1 <= month <= 12):
            raise ReportError("month must be between 1 and 12")
        if not (1970 <= year <= 9999):
            raise ReportError("year is out of range")
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
    else:
        first_this_month = local_now.date().replace(day=1)
        end = first_this_month - timedelta(days=1)
        start = end.replace(day=1)

    start_at = datetime.combine(start, dtime(0, 0)).replace(tzinfo=zone).astimezone(
        timezone.utc
    )
    end_at = datetime.combine(end + timedelta(days=1), dtime(0, 0)).replace(
        tzinfo=zone
    ).astimezone(timezone.utc)
    return ReportPeriod(start=start, end=end, start_at=start_at, end_at=end_at)


def period_for_config(
    config: Any,
    *,
    now: datetime | None = None,
    year: int | None = None,
    month: int | None = None,
) -> ReportPeriod:
    return resolve_period(
        resolve_zone(config.report_timezone), now=now, year=year, month=month
    )


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class ReportService:
    """Generates, stores, edits, and publishes monthly reports."""

    def __init__(self, config_service: ConfigService) -> None:
        self._config = config_service

    # -- generate ----------------------------------------------------------

    async def generate(
        self,
        guild: discord.Guild,
        *,
        period: ReportPeriod | None = None,
        prepared_by: str = "Candy",
        year: int | None = None,
        month: int | None = None,
    ) -> dict[str, Any]:
        config = self._config.get(guild.id)
        period = period or period_for_config(config, year=year, month=month)

        summary = await asyncio.to_thread(self._fetch_summary, guild.id, period)
        summary = self._decorate(summary, guild, period, prepared_by)

        existing = await asyncio.to_thread(
            self._load_row, guild.id, period.start.isoformat()
        )
        narrative = self._narrative_from(existing)
        markdown = render_report(
            summary,
            narrative,
            guild_name=guild.name,
            period=period,
            prepared_by=prepared_by,
        )

        payload: dict[str, Any] = {
            "guild_id": guild.id,
            "period_start": period.start.isoformat(),
            "period_end": period.end.isoformat(),
            "status": (existing or {}).get("status") or "draft",
            "prepared_by": prepared_by,
            "auto_data": summary,
            "narrative": narrative,
            "markdown": markdown,
        }
        if existing:
            # Regenerating must not silently unpublish a report or lose the
            # link to the message that is already in the channel.
            for key in (
                "published_at",
                "published_channel_id",
                "published_message_id",
            ):
                payload[key] = existing.get(key)

        row = await asyncio.to_thread(self._upsert_row, payload)
        log.info(
            "Generated report for guild %s period %s (%s)",
            guild.id,
            period.start,
            period.end,
        )
        return row

    async def preview(
        self,
        guild: discord.Guild,
        *,
        period: ReportPeriod | None = None,
        prepared_by: str = "Candy",
        year: int | None = None,
        month: int | None = None,
    ) -> tuple[ReportPeriod, str]:
        """Render the report WITHOUT saving it or posting it.

        ``/report view`` shows a stored row; this shows what ``generate`` would
        produce right now, which is what makes it safe to check the numbers and
        the stored narrative before committing a draft. Nothing is written, so
        a preview can never overwrite a published report.
        """
        config = self._config.get(guild.id)
        period = period or period_for_config(config, year=year, month=month)
        summary = await asyncio.to_thread(self._fetch_summary, guild.id, period)
        summary = self._decorate(summary, guild, period, prepared_by)
        existing = await asyncio.to_thread(
            self._load_row, guild.id, period.start.isoformat()
        )
        markdown = render_report(
            summary,
            self._narrative_from(existing),
            guild_name=guild.name,
            period=period,
            prepared_by=prepared_by,
        )
        return period, markdown

    def _fetch_summary(self, guild_id: int, period: ReportPeriod) -> dict[str, Any]:
        params: dict[str, Any] = {
            "p_guild_id": guild_id,
            "p_start": period.start.isoformat(),
            "p_end": period.end.isoformat(),
        }
        if period.start_at is not None:
            params["p_start_at"] = period.start_at.isoformat()
        if period.end_at is not None:
            params["p_end_at"] = period.end_at.isoformat()
        try:
            result = supabase_client.call_rpc("report_summary", params)
        except Exception as exc:  # noqa: BLE001
            raise ReportError(f"could not read activity data: {exc}") from exc
        return dict(result or {})

    def _decorate(
        self,
        summary: dict[str, Any],
        guild: discord.Guild,
        period: ReportPeriod,
        prepared_by: str,
    ) -> dict[str, Any]:
        """Add identity, refresh names from Discord, and note data gaps."""
        summary = dict(summary or {})
        summary["guild_id"] = guild.id
        summary["guild_name"] = guild.name
        summary["period_start"] = period.start.isoformat()
        summary["period_end"] = period.end.isoformat()
        summary["period_days"] = period.days
        summary["prepared_by"] = prepared_by
        summary["generated_at"] = datetime.now(timezone.utc).isoformat()
        summary["member_current"] = guild.member_count
        summary["boost_count_current"] = guild.premium_subscription_count

        # Stored names are snapshots. A member who renamed since the activity
        # was recorded should be shown under their current name.
        for key, id_field, name_field in (
            ("top_message_users", "user_id", "user_name"),
            ("top_voice_users", "user_id", "user_name"),
            ("staff_actions", "moderator_id", "moderator_name"),
        ):
            for item in summary.get(key) or []:
                if not isinstance(item, dict):
                    continue
                member_id = item.get(id_field)
                member = guild.get_member(int(member_id)) if member_id else None
                if member is not None:
                    item[name_field] = member.display_name

        for key in ("top_channels", "top_voice_channels"):
            for item in summary.get(key) or []:
                if not isinstance(item, dict):
                    continue
                channel_id = item.get("channel_id")
                channel = guild.get_channel(int(channel_id)) if channel_id else None
                if channel is not None:
                    item["channel_name"] = getattr(channel, "name", None)

        notes: list[str] = []
        tracked = _as_int(summary.get("days_tracked"))
        if tracked is not None and tracked < period.days:
            notes.append(
                f"Activity data covers {tracked} of {period.days} days; gaps reflect "
                "bot downtime or the period before tracking was enabled."
            )
        if summary.get("member_start") is None or summary.get("member_end") is None:
            notes.append(
                "Member growth could not be calculated because a daily member "
                "snapshot is missing for the start or end of the period."
            )
        elif tracked is not None and tracked < period.days:
            # The RPC takes the earliest/latest recorded snapshot, not the
            # first/last day of the month, so a gap at a boundary makes the
            # start/end figures "closest available" rather than exact.
            notes.append(
                "Member counts use the closest recorded day, which may not be "
                "the first or last day of the period."
            )
        if tracked == 0:
            notes.append("No activity was recorded for this period.")
        notes.append(
            "Leaves include members who were kicked or banned, because Discord "
            "does not distinguish them in the leave event."
        )
        notes.append(
            "Warnings are read only from Dyno mod-log embeds; timeouts are "
            "detected live and can be missed while the bot is offline."
        )
        summary["data_notes"] = notes
        return summary

    # -- load / edit -------------------------------------------------------

    async def load(self, guild_id: int, period_start: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._load_row, guild_id, period_start)

    async def list_reports(self, guild_id: int, limit: int = 12) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_rows, guild_id, limit)

    async def update_narrative(
        self,
        guild_id: int,
        period_start: str,
        field: str,
        text: str,
    ) -> dict[str, Any]:
        if field not in NARRATIVE_FIELDS:
            raise ReportError(f"unknown narrative field: {field}")

        row = await asyncio.to_thread(self._load_row, guild_id, period_start)
        if row is None:
            raise ReportError("no report exists for that period; generate it first")

        narrative = self._narrative_from(row)
        narrative[field] = (text or "").strip()

        auto_data = dict(row.get("auto_data") or {})
        period = _period_from_row(row)
        guild_name = str(auto_data.get("guild_name") or "this server")
        markdown = render_report(
            auto_data,
            narrative,
            guild_name=guild_name,
            period=period,
            prepared_by=row.get("prepared_by"),
        )

        payload = {
            "guild_id": guild_id,
            "period_start": period.start.isoformat(),
            "period_end": period.end.isoformat(),
            "status": row.get("status") or "draft",
            "prepared_by": row.get("prepared_by"),
            "auto_data": auto_data,
            "narrative": narrative,
            "markdown": markdown,
            "published_at": row.get("published_at"),
            "published_channel_id": row.get("published_channel_id"),
            "published_message_id": row.get("published_message_id"),
        }
        return await asyncio.to_thread(self._upsert_row, payload)

    # -- publish -----------------------------------------------------------

    async def publish(
        self,
        row: dict[str, Any],
        channel: discord.abc.Messageable,
        *,
        channel_id: int | None = None,
    ) -> PublishResult:
        """Post the report and mark the row published.

        If this report was already published to the same channel, the existing
        message is EDITED instead of a second copy being posted. Editing leaves
        a narrative change visible where people already read the report; posting
        again would leave the old, stale copy above it.

        A report longer than Discord's 2000-character message limit is sent as
        a ``.md`` attachment instead of being split into a dozen messages. That
        keeps the document intact and is why the bot needs Attach Files.
        """
        markdown = str(row.get("markdown") or "").strip()
        if not markdown:
            raise ReportError("the report has no rendered markdown; regenerate it")

        period_start = str(row.get("period_start") or "")
        content = f"📊 **Monthly report — {period_start[:7]}**"
        allowed = discord.AllowedMentions.none()
        inline = len(markdown) <= 2000
        target_id = channel_id or getattr(channel, "id", None)

        def attachment() -> discord.File:
            # A fresh File each attempt: a File consumed by a failed request
            # cannot be re-sent at its current position.
            return discord.File(
                io.BytesIO(markdown.encode("utf-8")),
                filename=f"report-{period_start or 'month'}.md",
            )

        message: discord.Message | None = None
        edited = False

        existing_message_id = row.get("published_message_id")
        existing_channel_id = row.get("published_channel_id")
        fetch = getattr(channel, "fetch_message", None)
        can_edit = (
            fetch is not None
            and existing_message_id is not None
            and existing_channel_id is not None
            and target_id is not None
            and int(existing_channel_id) == int(target_id)
        )
        if can_edit:
            try:
                message = await fetch(int(existing_message_id))
                if inline:
                    # attachments=[] removes the old .md when a report that
                    # used to be long now fits in a single message.
                    await message.edit(
                        content=markdown, attachments=[], allowed_mentions=allowed
                    )
                else:
                    await message.edit(
                        content=content,
                        attachments=[attachment()],
                        allowed_mentions=allowed,
                    )
                edited = True
            except discord.NotFound:
                # The message was deleted; fall through and post a new one.
                message = None
                log.info(
                    "Published report message %s was deleted; posting a new copy",
                    existing_message_id,
                )
            except discord.HTTPException as exc:
                message = None
                log.warning(
                    "Could not edit report message %s (%s); posting a new copy",
                    existing_message_id,
                    exc,
                )

        if message is None:
            try:
                if inline:
                    message = await channel.send(
                        content=markdown, allowed_mentions=allowed
                    )
                else:
                    message = await channel.send(
                        content=content, file=attachment(), allowed_mentions=allowed
                    )
            except discord.Forbidden as exc:
                raise ReportError(
                    "I need Send Messages and (for a long report) Attach Files in that channel."
                ) from exc
            except discord.HTTPException as exc:
                raise ReportError(f"Discord rejected the report message: {exc}") from exc

        payload = {
            "guild_id": int(row["guild_id"]),
            "period_start": period_start,
            "period_end": row.get("period_end"),
            "status": "published",
            "prepared_by": row.get("prepared_by"),
            "auto_data": row.get("auto_data") or {},
            "narrative": row.get("narrative") or {},
            "markdown": markdown,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "published_channel_id": target_id,
            "published_message_id": message.id,
        }
        stored = await asyncio.to_thread(self._upsert_row, payload)
        return PublishResult(row=stored, edited=edited)

    async def delivery_probe(self, guild: discord.Guild) -> dict[str, Any]:
        """Health check for ``/report test``: read the current month so far.

        ``report_summary`` touches every report table, so a missing schema, a
        revoked grant, or a broken RPC fails HERE with a clear message instead
        of silently at the scheduled minute. The summary it returns is live
        data for the current month-to-date, so the test message can also say
        how much has been collected so far.
        """
        config = self._config.get(guild.id)
        zone = resolve_zone(config.report_timezone)
        now = datetime.now(timezone.utc)
        local_today = now.astimezone(zone).date()
        period = ReportPeriod(
            start=local_today.replace(day=1),
            end=local_today,
            start_at=datetime.combine(local_today.replace(day=1), dtime(0, 0))
            .replace(tzinfo=zone)
            .astimezone(timezone.utc),
            end_at=now,
        )
        return await asyncio.to_thread(self._fetch_summary, guild.id, period)

    # -- database helpers (blocking) ---------------------------------------

    @staticmethod
    def _load_row(guild_id: int, period_start: str) -> dict[str, Any] | None:
        return supabase_client.select_one(
            "guild_reports",
            filters={"guild_id": guild_id, "period_start": period_start},
        )

    @staticmethod
    def _list_rows(guild_id: int, limit: int) -> list[dict[str, Any]]:
        return supabase_client.select(
            "guild_reports",
            "id,guild_id,period_start,period_end,status,prepared_by,published_at,updated_at",
            filters={"guild_id": guild_id},
            order_by="period_start.desc",
            limit=max(1, min(limit, 50)),
        )

    @staticmethod
    def _upsert_row(payload: dict[str, Any]) -> dict[str, Any]:
        rows = supabase_client.upsert(
            "guild_reports", payload, on_conflict="guild_id,period_start"
        )
        return rows[0] if rows else payload

    @staticmethod
    def _narrative_from(row: dict[str, Any] | None) -> dict[str, str]:
        if not row:
            return {}
        raw = row.get("narrative") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if v is not None}


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render_report(
    summary: dict[str, Any],
    narrative: dict[str, Any],
    *,
    guild_name: str,
    period: ReportPeriod,
    prepared_by: str | None = None,
) -> str:
    """Render the seven-section markdown report.

    Pure and deterministic so tests can feed a fake summary. Missing numbers
    render as ``—`` rather than ``None``; missing narrative renders as
    ``_(not provided)_`` so a draft is obviously incomplete instead of looking
    finished.
    """
    summary = summary or {}
    narrative = narrative or {}

    member_start = _as_int(summary.get("member_start"))
    member_end = _as_int(summary.get("member_end"))
    member_growth = _as_int(summary.get("member_growth"))
    if member_growth is None and member_start is not None and member_end is not None:
        member_growth = member_end - member_start

    total_messages = _as_int(summary.get("total_messages")) or 0
    total_voice = _as_int(summary.get("total_voice_seconds")) or 0
    period_days = period.days
    days_tracked = _as_int(summary.get("days_tracked"))
    # Average over the days that were actually observed. Dividing by calendar
    # days turns bot downtime into "members got quieter", which reads as a real
    # trend when it is only a gap in collection.
    observed_days = days_tracked if days_tracked and days_tracked > 0 else period_days
    avg_messages = round(total_messages / observed_days) if observed_days else 0
    avg_voice = round(total_voice / observed_days) if observed_days else 0
    observed_note = (
        f" (over {observed_days} observed days)"
        if days_tracked is not None and observed_days != period_days
        else ""
    )
    online_seconds = _as_int(summary.get("bot_online_seconds"))
    uptime_pct = None
    if online_seconds is not None and observed_days:
        # bot_online_seconds is accumulated between flushes, so it is an
        # approximate uptime rather than an exact one.
        uptime_pct = max(
            0, min(100, round(100 * online_seconds / (observed_days * 86400)))
        )

    moderation = summary.get("moderation") or {}
    events = summary.get("events") or {}
    data_notes = [str(n) for n in (summary.get("data_notes") or []) if n]

    lines: list[str] = []
    add = lines.append

    add(f"# Monthly Server Report — {guild_name}")
    add("")
    add(f"**Period:** {_fmt_date(period.start)} – {_fmt_date(period.end)}")
    add(f"**Prepared by:** {prepared_by or '—'}")
    generated = summary.get("generated_at")
    if generated:
        add(f"**Generated:** {_fmt_datetime(generated)}")
    add("")
    add("---")
    add("")

    # 1 -------------------------------------------------------------------
    add("## 1. Server Overview")
    add("")
    add(f"- **Members at start:** {_fmt_num(member_start)}")
    add(f"- **Members at end:** {_fmt_num(member_end)}")
    add(f"- **Members now:** {_fmt_num(summary.get('member_current'))}")
    add(f"- **Net growth:** {_fmt_delta(member_growth)}")
    add(f"- **Joins:** {_fmt_num(summary.get('total_joins'))}")
    add(f"- **Leaves:** {_fmt_num(summary.get('total_leaves'))}")
    add(f"- **Current boosts:** {_fmt_num(_first_not_none(summary.get('boost_count_current'), summary.get('boost_count')))}")
    add(f"- **Boosts gained:** {_fmt_num(summary.get('boosts_gained'))}")
    add(f"- **Boosts lost:** {_fmt_num(summary.get('boosts_lost'))}")
    if data_notes:
        add("")
        add("> **Data notes:** " + " ".join(data_notes))
    add("")

    # 2 -------------------------------------------------------------------
    add("## 2. Server Activity")
    add("")
    add("### Messages")
    add(f"- **Total messages:** {_fmt_num(total_messages)}")
    add(f"- **Average per day:** {_fmt_num(avg_messages)}{observed_note}")
    add(f"- **Most active day:** {_fmt_active_day(summary.get('most_active_day'))}")
    add(f"- **Most active hour:** {_fmt_active_hour(summary.get('most_active_hour'))}")
    add("")
    add("### Voice")
    add(f"- **Total voice time:** {_fmt_duration(total_voice)}")
    add(f"- **Voice joins:** {_fmt_num(summary.get('voice_joins'))}")
    add(f"- **Average per day:** {_fmt_duration(avg_voice)}{observed_note}")
    add("")
    add("### Data Coverage")
    tracked_label = "—" if days_tracked is None else _fmt_num(days_tracked)
    add(f"- **Days observed:** {tracked_label} of {_fmt_num(period_days)}")
    add(f"- **Bot uptime (approx.):** {_fmt_pct(uptime_pct)}")
    add("")
    add("### Most Active Members — Messages")
    add("")
    message_users = summary.get("top_message_users") or []
    if message_users:
        for index, item in enumerate(message_users, start=1):
            if not isinstance(item, dict):
                continue
            add(
                f"{index}. **{_person(item.get('user_name'), item.get('user_id'))}** — "
                f"{_fmt_num(item.get('messages'))} messages"
            )
    else:
        add("_No message activity recorded._")
    add("")
    add("### Most Active Members — Voice")
    add("")
    voice_users = summary.get("top_voice_users") or []
    if voice_users:
        for index, item in enumerate(voice_users, start=1):
            if not isinstance(item, dict):
                continue
            add(
                f"{index}. **{_person(item.get('user_name'), item.get('user_id'))}** — "
                f"{_fmt_duration(_as_int(item.get('voice_seconds')) or 0)}"
            )
    else:
        add("_No voice activity recorded._")
    add("")
    add("### Most Active Text Channels")
    add("")
    text_channels = summary.get("top_channels") or []
    if text_channels:
        for item in text_channels:
            if not isinstance(item, dict):
                continue
            add(
                f"- **{_channel(item)}** — {_fmt_num(item.get('messages'))} messages"
            )
    else:
        add("_No channel activity recorded._")
    add("")
    add("### Most Active Voice Channels")
    add("")
    voice_channels = summary.get("top_voice_channels") or []
    if voice_channels:
        for item in voice_channels:
            if not isinstance(item, dict):
                continue
            add(
                f"- **{_channel(item)}** — "
                f"{_fmt_duration(_as_int(item.get('voice_seconds')) or 0)}"
            )
    else:
        add("_No voice channel activity recorded._")
    add("")

    # 3 -------------------------------------------------------------------
    add("## 3. Community Highlights")
    add("")
    _narrative_section(add, narrative, "notable_contributions")
    _narrative_section(add, narrative, "community_achievements")
    _narrative_section(add, narrative, "positive_feedback")

    # 4 -------------------------------------------------------------------
    add("## 4. Staff & Moderation")
    add("")
    add("### Moderation Summary")
    add(f"- **Warnings:** {_fmt_num(moderation.get('warns'))}")
    add(f"- **Timeouts:** {_fmt_num(moderation.get('timeouts'))}")
    add(f"- **Kicks:** {_fmt_num(moderation.get('kicks'))}")
    add(f"- **Bans:** {_fmt_num(moderation.get('bans'))}")
    add(f"- **Unbans:** {_fmt_num(moderation.get('unbans'))}")
    add(f"- **User reports (ticket type):** {_fmt_num(moderation.get('reports'))}")
    add(f"- **Issued via Dyno:** {_fmt_num(moderation.get('via_dyno'))}")
    add(f"- **Issued natively:** {_fmt_num(moderation.get('via_native'))}")
    add("")
    add("### Tickets Logged")
    add("")
    ticket_total = _as_int(moderation.get("tickets_logged")) or 0
    add(f"- **Total tickets logged:** {_fmt_num(ticket_total)}")
    by_type = moderation.get("tickets_by_type")
    if not isinstance(by_type, dict):
        by_type = {}
    # Every known type is listed, including zeroes: "Partnership: 0" is useful
    # information, while a missing row just looks like the report forgot it.
    # Any unexpected label is still shown rather than silently dropped, so the
    # type counts always add up to the total.
    labels = list(TICKET_TYPE_ORDER)
    labels.extend(sorted(key for key in by_type if key not in TICKET_TYPE_ORDER))
    for label in labels:
        add(f"- **{label}:** {_fmt_num(_as_int(by_type.get(label)) or 0)}")
    add("")
    add("### Staff Actions")
    add("")
    staff = summary.get("staff_actions") or []
    if staff:
        for item in staff:
            if not isinstance(item, dict):
                continue
            add(
                f"- **{_person(item.get('moderator_name'), item.get('moderator_id'))}** — "
                f"{_fmt_num(item.get('actions'))} action(s)"
            )
    else:
        add("_No staff moderation actions recorded._")
    add("")
    _narrative_section(add, narrative, "staff_issues")

    # 5 -------------------------------------------------------------------
    add("## 5. Events & Activities")
    add("")
    add(f"- **Events hosted:** {_fmt_num(events.get('hosted'))}")
    add(f"- **Total participation:** {_fmt_num(events.get('participation'))}")
    add(f"- **Completed successfully:** {_fmt_num(events.get('successful'))}")
    add("")
    add("### Upcoming Events")
    add("")
    upcoming = events.get("upcoming") or []
    if upcoming:
        for item in upcoming:
            if not isinstance(item, dict):
                continue
            when = item.get("start_time")
            stamp = ""
            if when:
                try:
                    moment = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
                    stamp = f" — <t:{int(moment.timestamp())}:f>"
                except ValueError:
                    stamp = f" — {when}"
            status = item.get("status") or "scheduled"
            add(f"- **{item.get('name') or 'Untitled event'}**{stamp} ({status})")
    else:
        add("_No upcoming events._")
    add("")

    # 6 -------------------------------------------------------------------
    add("## 6. Issues & Improvements")
    add("")
    _narrative_section(add, narrative, "problems_faced")
    _narrative_section(add, narrative, "complaints")
    _narrative_section(add, narrative, "suggestions")
    _narrative_section(add, narrative, "actions_taken")
    _narrative_section(add, narrative, "improvements_needed")

    # 7 -------------------------------------------------------------------
    add("## 7. Final Summary")
    add("")
    final_text = str(narrative.get("final_summary") or "").strip()
    add(final_text or _auto_final_summary(summary))
    add("")
    _narrative_section(add, narrative, "personal_opinions")
    _narrative_section(add, narrative, "extra")

    return "\n".join(lines).strip() + "\n"


def _narrative_section(add, narrative: dict[str, Any], key: str) -> None:
    add(f"### {NARRATIVE_FIELDS[key]}")
    add("")
    text = str(narrative.get(key) or "").strip()
    add(text or NOT_PROVIDED)
    add("")


def _auto_final_summary(summary: dict[str, Any]) -> str:
    """A factual fallback when nobody wrote a Final Summary.

    It deliberately does not invent sentiment or praise; it states the numbers
    the report already contains so the section is never empty.
    """
    parts: list[str] = []
    member_end = _as_int(summary.get("member_end"))
    member_growth = _as_int(summary.get("member_growth"))
    if member_end is not None:
        growth = f" ({member_growth:+d})" if member_growth is not None else ""
        parts.append(f"The server ended the period with {_fmt_num(member_end)} members{growth}.")

    messages = _as_int(summary.get("total_messages")) or 0
    voice = _as_int(summary.get("total_voice_seconds")) or 0
    if messages or voice:
        parts.append(
            f"Members sent {_fmt_num(messages)} messages and spent "
            f"{_fmt_duration(voice)} in voice channels."
        )

    moderation = summary.get("moderation") or {}
    actions = sum(
        _as_int(moderation.get(k)) or 0
        for k in ("warns", "timeouts", "kicks", "bans", "unbans")
    )
    if actions:
        parts.append(f"Staff recorded {_fmt_num(actions)} moderation action(s).")

    events = summary.get("events") or {}
    hosted = _as_int(events.get("hosted")) or 0
    if hosted:
        parts.append(f"{_fmt_num(hosted)} event(s) were hosted.")

    return " ".join(parts) or "No activity was recorded for this period."


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _fmt_num(value: Any) -> str:
    number = _as_int(value)
    return "—" if number is None else f"{number:,}"


def _fmt_pct(value: Any) -> str:
    number = _as_int(value)
    return "—" if number is None else f"{number}%"


def _fmt_delta(value: Any) -> str:
    number = _as_int(value)
    if number is None:
        return "—"
    return f"{number:+,}"


def _fmt_duration(seconds: Any) -> str:
    total = _as_int(seconds) or 0
    if total <= 0:
        return "0m"
    minutes = total // 60
    hours = minutes // 60
    days = hours // 24
    if days:
        return f"{days}d {hours % 24}h {minutes % 60}m"
    if hours:
        return f"{hours}h {minutes % 60}m"
    return f"{minutes}m"


def _fmt_date(value: Any) -> str:
    if isinstance(value, date):
        return f"{value:%d %b %Y}"
    try:
        return f"{date.fromisoformat(str(value)[:10]):%d %b %Y}"
    except (TypeError, ValueError):
        return str(value) if value else "—"


def _fmt_datetime(value: Any) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return f"{moment.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC"


def _fmt_active_day(value: Any) -> str:
    if not isinstance(value, dict):
        return "—"
    day = _fmt_date(value.get("day"))
    return f"{day} ({_fmt_num(value.get('messages'))} messages)"


def _fmt_active_hour(value: Any) -> str:
    if not isinstance(value, dict):
        return "—"
    hour = _as_int(value.get("hour"))
    if hour is None:
        return "—"
    return f"{hour:02d}:00–{(hour + 1) % 24:02d}:00 local ({_fmt_num(value.get('messages'))} messages)"


def _person(name: Any, user_id: Any) -> str:
    text = str(name or "").strip()
    if text:
        return discord.utils.escape_markdown(text)
    identifier = _as_int(user_id)
    return f"User {identifier}" if identifier is not None else "Unknown user"


def _channel(item: dict[str, Any]) -> str:
    name = str(item.get("channel_name") or "").strip()
    if name:
        return "#" + discord.utils.escape_markdown(name.lstrip("#"))
    identifier = _as_int(item.get("channel_id"))
    return f"#channel-{identifier}" if identifier is not None else "#unknown-channel"


def _period_from_row(row: dict[str, Any]) -> ReportPeriod:
    start = date.fromisoformat(str(row.get("period_start"))[:10])
    end = date.fromisoformat(str(row.get("period_end"))[:10])
    return ReportPeriod(start=start, end=end)


__all__ = [
    "NARRATIVE_FIELDS",
    "ReportError",
    "ReportPeriod",
    "ReportService",
    "render_report",
    "resolve_period",
    "period_for_config",
]
