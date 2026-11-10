"""Monthly report commands: /report and /config report.

The scheduler (services/report_scheduler.py) decides WHEN an automatic report
runs. This cog owns the command surface and the Discord delivery step, because
both are presentation concerns.

Reports are generated on demand as DRAFTS. Nothing is posted until an admin
publishes it, except the opt-in automatic monthly run. Narrative sections are
stored in the report row and survive regeneration, so an admin can write the
human parts first and regenerate the numbers later.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from cogs import bind_group_commands
from config import settings
from db.config_cache import GuildConfig
from services.config_service import ConfigError, ConfigService, TimezoneError
from services.report_scheduler import ReportScheduler, parse_report_time
from services.report_service import (
    NARRATIVE_FIELDS,
    ReportError,
    ReportPeriod,
    ReportService,
    period_for_config,
)
from utils.logging_setup import get_logger

log = get_logger("cogs.report")

MANAGE_GUILD_PERMS = discord.Permissions(manage_guild=True)

NARRATIVE_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, label in NARRATIVE_FIELDS.items()
]

# --- command groups (module level: no shadowing) ----------------------------

report_group = app_commands.Group(
    name="report",
    description="Generate and publish the monthly server report.",
    default_permissions=MANAGE_GUILD_PERMS,
)


def _build_config_report_group() -> app_commands.Group:
    from cogs.config_cog import config_group

    return app_commands.Group(
        name="report",
        description="Monthly report settings.",
        parent=config_group,
    )


config_report_group = _build_config_report_group()


class ReportCog(commands.Cog, name="Report"):
    """Monthly report generation, editing, publishing, and scheduling."""

    def __init__(self, bot: commands.Bot, config_service: ConfigService) -> None:
        self.bot = bot
        self.config = config_service
        self.service = ReportService(config_service)
        self.scheduler = ReportScheduler(
            config_service=config_service,
            runner=self._auto_generate,
            get_guild=bot.get_guild,
        )

    async def cog_load(self) -> None:
        self.scheduler.start()

    async def cog_unload(self) -> None:
        await self.scheduler.stop()

    async def shutdown(self) -> None:
        """Stop the scheduler. Called by CandyBot.close()."""
        await self.scheduler.stop()

    # ------------------------------------------------------------------
    # automatic posting (called by the scheduler)
    # ------------------------------------------------------------------

    async def _auto_generate(
        self, guild: discord.Guild, config: GuildConfig, period: ReportPeriod
    ) -> bool:
        channel_id = config.report_channel_id
        if channel_id is None:
            return False
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            log.warning(
                "Guild %s: report channel %s is missing or not messageable",
                guild.id,
                channel_id,
            )
            return False

        row = await self.service.generate(
            guild, period=period, prepared_by="Candy (automatic)"
        )
        await self.service.publish(row, channel, channel_id=channel_id)
        log.info(
            "Auto-published report for guild %s (%s) to channel %s",
            guild.id,
            period.label,
            channel_id,
        )
        return True

    # ------------------------------------------------------------------
    # /report
    # ------------------------------------------------------------------

    @report_group.command(
        name="generate", description="Build a draft report for a month (previous month by default)."
    )
    @app_commands.describe(
        year="Year, e.g. 2026. Leave both year and month empty for last month.",
        month="Month 1-12. Leave both year and month empty for last month.",
    )
    async def generate(
        self,
        interaction: discord.Interaction,
        year: int | None = None,
        month: int | None = None,
    ) -> None:
        guild = await self._guild(interaction)
        if guild is None:
            return

        period = await self._period(interaction, guild, year, month)
        if period is None:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self.service.generate(
                guild, period=period, prepared_by=interaction.user.display_name
            )
        except ReportError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            log.error("Report generation failed: %s", exc, exc_info=True)
            await interaction.followup.send(
                "Something went wrong generating the report. Check the bot logs.",
                ephemeral=True,
            )
            return

        summary = row.get("auto_data") or {}
        lines = [
            f"✅ Draft generated for **{period.label}**.",
            f"Messages: **{_num(summary.get('total_messages'))}**  •  "
            f"Voice: **{_duration(summary.get('total_voice_seconds'))}**",
            f"Warnings: **{_num((summary.get('moderation') or {}).get('warns'))}**  •  "
            f"Bans: **{_num((summary.get('moderation') or {}).get('bans'))}**",
            "",
            "Fill in the human sections with `/report edit`, then `/report publish`.",
        ]
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @report_group.command(name="view", description="Preview a report without posting it.")
    @app_commands.describe(
        year="Year, e.g. 2026. Leave both year and month empty for last month.",
        month="Month 1-12. Leave both year and month empty for last month.",
    )
    async def view(
        self,
        interaction: discord.Interaction,
        year: int | None = None,
        month: int | None = None,
    ) -> None:
        guild = await self._guild(interaction)
        if guild is None:
            return

        period = await self._period(interaction, guild, year, month)
        if period is None:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.service.load(guild.id, period.start.isoformat())
        if row is None:
            await interaction.followup.send(
                f"No report exists for **{period.label}**. "
                "Generate it with `/report generate`.",
                ephemeral=True,
            )
            return

        await self._send_markdown(
            interaction.followup,
            str(row.get("markdown") or ""),
            content=f"📄 **Report — {period.label}**",
            filename=f"report-{period.start.isoformat()}.md",
        )

    @report_group.command(
        name="publish", description="Post a report to the report channel (or another channel)."
    )
    @app_commands.describe(
        year="Year, e.g. 2026. Leave both year and month empty for last month.",
        month="Month 1-12. Leave both year and month empty for last month.",
        channel="Where to post. Defaults to the configured report channel.",
    )
    async def publish(
        self,
        interaction: discord.Interaction,
        year: int | None = None,
        month: int | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = await self._guild(interaction)
        if guild is None:
            return

        period = await self._period(interaction, guild, year, month)
        if period is None:
            return

        config = self.config.get(guild.id)
        target = channel
        if target is None and config.report_channel_id is not None:
            target = guild.get_channel(config.report_channel_id)  # type: ignore[assignment]
        if target is None and isinstance(interaction.channel, discord.TextChannel):
            target = interaction.channel
        if not isinstance(target, discord.abc.Messageable):
            await interaction.response.send_message(
                "No report channel is configured. Set one with "
                "`/config report channel`, or pass a channel here.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.service.load(guild.id, period.start.isoformat())
        if row is None:
            try:
                row = await self.service.generate(
                    guild, period=period, prepared_by=interaction.user.display_name
                )
            except ReportError as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
                return

        try:
            published = await self.service.publish(
                row, target, channel_id=getattr(target, "id", None)
            )
        except ReportError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            log.error("Report publish failed: %s", exc, exc_info=True)
            await interaction.followup.send(
                "Something went wrong posting the report. Check the bot logs.",
                ephemeral=True,
            )
            return

        url = _message_url(published, guild.id)
        where = getattr(target, "mention", None) or "the channel"
        await interaction.followup.send(
            f"✅ Published **{period.label}** to {where}." + (f"\n{url}" if url else ""),
            ephemeral=True,
        )

    @report_group.command(
        name="edit", description="Write or replace one of the report's human sections."
    )
    @app_commands.describe(
        field="Which section to edit.",
        text="The new text for that section (up to 2000 characters).",
        year="Year, e.g. 2026. Leave both year and month empty for last month.",
        month="Month 1-12. Leave both year and month empty for last month.",
    )
    @app_commands.choices(field=NARRATIVE_CHOICES)
    async def edit(
        self,
        interaction: discord.Interaction,
        field: app_commands.Choice[str],
        text: str,
        year: int | None = None,
        month: int | None = None,
    ) -> None:
        guild = await self._guild(interaction)
        if guild is None:
            return

        period = await self._period(interaction, guild, year, month)
        if period is None:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.service.update_narrative(
                guild.id, period.start.isoformat(), field.value, text
            )
        except ReportError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            log.error("Narrative update failed: %s", exc, exc_info=True)
            await interaction.followup.send(
                "Something went wrong saving that section. Check the bot logs.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Saved **{field.name}** for **{period.label}**.",
            ephemeral=True,
        )

    @report_group.command(name="list", description="List recently generated reports.")
    @app_commands.describe(limit="How many to show (default 10).")
    async def list_reports(self, interaction: discord.Interaction, limit: int = 10) -> None:
        guild = await self._guild(interaction)
        if guild is None:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self.service.list_reports(guild.id, max(1, min(limit, 25)))
        if not rows:
            await interaction.followup.send(
                "No reports yet. Start with `/report generate`.", ephemeral=True
            )
            return

        lines = []
        for row in rows:
            period = str(row.get("period_start") or "")[:7]
            status = row.get("status") or "draft"
            marker = "✅" if status == "published" else "📝"
            lines.append(f"{marker} **{period}** — {status}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @report_group.command(
        name="test",
        description="Check the report setup and post a test message to the report channel.",
    )
    async def test(self, interaction: discord.Interaction) -> None:
        """Run the exact path a scheduled report takes, step by step.

        Every failure below is one the scheduler would otherwise hit silently
        at the scheduled minute, so each stops the test with an actionable
        message rather than a generic error.
        """
        guild = await self._guild(interaction)
        if guild is None:
            return

        config = self.config.get(guild.id)

        # -- 1. a channel is configured ---------------------------------
        channel_id = config.report_channel_id
        if channel_id is None:
            await interaction.response.send_message(
                "❌ No report channel is configured.\n"
                "Set one with `/config report channel #channel` first.",
                ephemeral=True,
            )
            return

        # -- 2. the channel still exists and can be posted in -------------
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message(
                f"❌ The configured report channel (id `{channel_id}`) was "
                "deleted, or is not a channel I can post in.\n"
                "Set a new one with `/config report channel #channel`.",
                ephemeral=True,
            )
            return

        # -- 3. the bot holds the permissions a report needs --------------
        me = guild.me
        if me is None:
            try:
                me = await guild.fetch_member(self.bot.user.id)
            except discord.HTTPException:
                me = None
        if me is not None:
            perms = channel.permissions_for(me)
            missing = [
                label
                for label, present in (
                    ("View Channels", perms.view_channel),
                    ("Send Messages", perms.send_messages),
                    ("Attach Files", perms.attach_files),
                )
                if not present
            ]
            if missing:
                await interaction.response.send_message(
                    "❌ I'm missing "
                    + ", ".join(f"**{m}**" for m in missing)
                    + f" in {channel.mention}.\n"
                    "An admin has to grant them, either in my server role or "
                    "in that channel's own permission overrides.",
                    ephemeral=True,
                )
                return

        # -- 4. the database answers (RPC, tables, grants) ----------------
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await self.service.delivery_probe(guild)
        except ReportError as exc:
            await interaction.followup.send(
                f"❌ Database check failed: {exc}\n"
                "Most likely cause: `supabase/schema.sql` (section 07c and "
                "the `report_summary` RPC) was not applied to the project.",
                ephemeral=True,
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.error("Report test database check failed: %s", exc, exc_info=True)
            await interaction.followup.send(
                "❌ Unexpected database error. Check the bot logs.", ephemeral=True
            )
            return

        # -- 5. a message actually lands in the channel ------------------
        days = _num(summary.get("days_tracked"))
        try:
            message = await channel.send(
                content=(
                    "🧪 **Report delivery test** — this channel will receive "
                    "the monthly server report.\n"
                    f"So far this month: **{_num(summary.get('total_messages'))}** "
                    f"messages across **{days}** tracked day(s)."
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            # Possible even after the check above: permission overrides can
            # differ between what the cache says and what Discord enforces.
            await interaction.followup.send(
                f"❌ Discord refused the test message in {channel.mention}: "
                "I lack Send Messages there. Check the channel's permission "
                "overrides.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Discord rejected the test message: {exc}", ephemeral=True
            )
            return

        # -- 6. success, plus anything worth knowing ----------------------
        url = f"https://discord.com/channels/{guild.id}/{channel.id}/{message.id}"
        try:
            days_count = int(summary.get("days_tracked") or 0)
        except (TypeError, ValueError):
            days_count = 0
        notes: list[str] = []
        if not config.report_enabled:
            notes.append(
                "The automatic schedule is **off** — arm it with "
                "`/config report toggle true`."
            )
        if not settings.activity_tracking_enabled:
            notes.append(
                "Activity tracking is disabled bot-wide "
                "(`ACTIVITY_TRACKING_ENABLED`), so nothing new is counted."
            )
        if days_count <= 0:
            notes.append(
                "No activity recorded for this month yet — the first report "
                "may look empty. That is normal right after setup."
            )

        lines = [f"✅ Test passed — message posted in {channel.mention}: <{url}>"]
        if notes:
            lines.append("")
            lines.extend(f"⚠️ {n}" for n in notes)
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------------------
    # /config report
    # ------------------------------------------------------------------

    @config_report_group.command(
        name="status", description="Show the monthly report schedule and data sources."
    )
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        config = self.config.get(interaction.guild_id)
        local_time = parse_report_time(config.report_time)
        time_text = local_time.strftime("%H:%M") if local_time else "not set"

        lines = [
            f"**Enabled:** {'yes' if config.report_enabled else 'no'}",
            f"**Channel:** {f'<#{config.report_channel_id}>' if config.report_channel_id else 'not set'}",
            f"**Schedule:** day **{config.report_day}** at **{time_text}** "
            f"({config.report_timezone})",
            f"**Last auto-generated:** {config.report_last_generated_period or 'never'}",
            "",
            "**Data sources**",
            f"Mod-log channel: "
            f"{f'<#{config.moderation_log_channel_id}>' if config.moderation_log_channel_id else 'not set (Dyno warnings will not be recorded)'}",
            f"Dyno bot ID: `{config.dyno_user_id}`",
            f"Staff roles: "
            f"{', '.join(f'<@&{r}>' for r in config.staff_role_ids) or 'not set'}",
            "",
            "Reports cover the **previous calendar month**. "
            "Use `/report generate` to build one on demand.",
        ]

        if config.report_timezone == "UTC":
            lines.append(
                "💡 **Timezone not set** (using UTC). Set it with "
                "`/config report timezone UTC+5:30` or an IANA name."
            )

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @config_report_group.command(
        name="toggle", description="Turn automatic monthly reports on or off."
    )
    @app_commands.describe(enabled="Enable or disable the automatic monthly report.")
    async def toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_report_enabled(gid, enabled),
            f"Automatic monthly reports are now **{'enabled' if enabled else 'disabled'}**.",
        )

    @config_report_group.command(
        name="channel", description="Set the channel automatic reports post in."
    )
    @app_commands.describe(channel="Leave empty to clear the report channel.")
    async def set_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_report_channel(gid, channel.id if channel else None),
            f"Reports will post in {channel.mention}." if channel else "Report channel cleared.",
        )

    @config_report_group.command(
        name="schedule", description="Set the day and time the monthly report runs."
    )
    @app_commands.describe(
        day="Day of the month, 1-28 (29-31 do not exist in every month).",
        hour="Hour, 0-23. Leave empty to keep the current hour.",
        minute="Minute, 0-59. Leave empty to keep the current minute.",
    )
    async def schedule(
        self,
        interaction: discord.Interaction,
        day: int,
        hour: int | None = None,
        minute: int | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        config = self.config.get(interaction.guild_id)
        current = parse_report_time(config.report_time)
        new_hour = hour if hour is not None else (current.hour if current else 9)
        new_minute = minute if minute is not None else (current.minute if current else 0)

        def apply_schedule(gid: int) -> None:
            # Both writes go through ConfigService; day first so an invalid day
            # raises before the time is changed.
            self.config.set_report_day(gid, day)
            self.config.set_report_time(gid, new_hour, new_minute)

        await self._apply(
            interaction,
            apply_schedule,
            f"Report schedule set to day **{day}** at **{new_hour:02d}:{new_minute:02d}** "
            f"({config.report_timezone}).",
        )

    @config_report_group.command(
        name="timezone", description="Set the timezone used for the report schedule."
    )
    @app_commands.describe(
        timezone_name="UTC+5:30 offset, or an IANA name like Asia/Kolkata"
    )
    async def set_timezone(
        self, interaction: discord.Interaction, timezone_name: str
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        try:
            await asyncio.to_thread(
                self.config.set_report_timezone, interaction.guild_id, timezone_name
            )
        except TimezoneError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        saved = self.config.get(interaction.guild_id).report_timezone
        note = ""
        if saved.startswith("UTC") and saved != "UTC":
            note = " (fixed offset - no daylight saving shifts)"
        await interaction.response.send_message(
            f"Report timezone set to **{saved}**.{note}", ephemeral=True
        )

    @config_report_group.command(
        name="modlog", description="Set the channel where Dyno posts moderation logs."
    )
    @app_commands.describe(channel="Leave empty to stop reading Dyno warnings.")
    async def set_modlog(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_moderation_log_channel(
                gid, channel.id if channel else None
            ),
            f"Mod-log channel set to {channel.mention}." if channel else "Mod-log channel cleared.",
        )

    @config_report_group.command(
        name="dyno", description="Set which bot is treated as Dyno for mod-log parsing."
    )
    @app_commands.describe(user="The Dyno bot user (default is Dyno's public bot).")
    async def set_dyno(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_dyno_user_id(gid, user.id),
            f"Dyno bot set to {user.mention} (`{user.id}`).",
        )

    @config_report_group.command(
        name="staffroles", description="Set which roles count as staff in the report."
    )
    @app_commands.describe(
        role1="A staff role.",
        role2="Another staff role.",
        role3="Another staff role.",
        role4="Another staff role.",
        role5="Another staff role.",
    )
    async def staff_roles(
        self,
        interaction: discord.Interaction,
        role1: discord.Role | None = None,
        role2: discord.Role | None = None,
        role3: discord.Role | None = None,
        role4: discord.Role | None = None,
        role5: discord.Role | None = None,
    ) -> None:
        roles = [role for role in (role1, role2, role3, role4, role5) if role is not None]
        await self._apply(
            interaction,
            lambda gid: self.config.set_staff_role_ids(gid, [r.id for r in roles]),
            (
                "Staff roles set to "
                + ", ".join(role.mention for role in roles)
                + "."
                if roles
                else "Staff roles cleared."
            ),
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _guild(self, interaction: discord.Interaction) -> discord.Guild | None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return None
        guild = self.bot.get_guild(interaction.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "I cannot see this server right now.", ephemeral=True
            )
            return None
        return guild

    async def _period(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        year: int | None,
        month: int | None,
    ) -> ReportPeriod | None:
        try:
            return period_for_config(
                self.config.get(guild.id), year=year, month=month
            )
        except ReportError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return None

    async def _send_markdown(
        self,
        target: Any,
        markdown: str,
        *,
        content: str,
        filename: str,
    ) -> None:
        """Send a report as a message, or as a .md file when it is too long."""
        if not markdown.strip():
            await target.send("That report has no rendered text. Regenerate it.")
            return
        if len(markdown) <= 2000:
            await target.send(content=markdown, allowed_mentions=discord.AllowedMentions.none())
            return
        file = discord.File(io.BytesIO(markdown.encode("utf-8")), filename=filename)
        await target.send(
            content=content,
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _apply(self, interaction: discord.Interaction, operation, success: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        try:
            await asyncio.to_thread(operation, interaction.guild_id)
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            log.error("Report config write failed: %s", exc, exc_info=True)
            await interaction.response.send_message(
                "Something went wrong saving that. Check the bot logs.", ephemeral=True
            )
            return
        await interaction.response.send_message(success, ephemeral=True)


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _duration(value: Any) -> str:
    try:
        total = int(value)
    except (TypeError, ValueError):
        return "—"
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


def _message_url(row: dict[str, Any], guild_id: int) -> str | None:
    channel_id = row.get("published_channel_id")
    message_id = row.get("published_message_id")
    if channel_id and message_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return None


async def setup(bot: commands.Bot) -> None:
    # The root /report group is module level, so it must be added explicitly.
    if bot.tree.get_command(report_group.name) is None:
        bot.tree.add_command(report_group)

    # config_report_group rides along with config_group. Ensure the root exists
    # even if this cog is loaded before config_cog in some future reordering.
    from cogs import config_cog

    root = bot.tree.get_command(config_cog.config_group.name)
    if root is None:
        bot.tree.add_command(config_cog.config_group)
    elif root is not config_cog.config_group:
        log.warning("A different 'config' command is already registered; leaving it alone")

    cog = ReportCog(bot, bot.config_service)  # type: ignore[attr-defined]
    await bot.add_cog(cog)
    n = bind_group_commands(cog)
    log.info("Bound %d report subcommand(s) to the cog instance", n)
