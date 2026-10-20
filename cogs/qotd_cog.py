"""Question of the Day: the /config qotd subtree and the posting callback.

The scheduler (services/qotd_scheduler.py) decides WHEN to fire. This cog owns
HOW the message is posted, because posting is a Discord concern.

Ordering guarantee: the scheduler calls back here first; the question is only
marked asked once the message is actually delivered. A failed post leaves the
question queued so the next tick retries inside the grace window.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from cogs import bind_group_commands
from db.config_cache import GuildConfig
from services.config_service import ConfigError, ConfigService
from services.qotd_scheduler import (
    QotdScheduler,
    next_occurrence,
    parse_local_time,
    resolve_zone,
)
from utils.logging_setup import get_logger

log = get_logger("cogs.qotd")

WEEKDAY_CHOICES = [
    app_commands.Choice(name="Monday", value=1),
    app_commands.Choice(name="Tuesday", value=2),
    app_commands.Choice(name="Wednesday", value=3),
    app_commands.Choice(name="Thursday", value=4),
    app_commands.Choice(name="Friday", value=5),
    app_commands.Choice(name="Saturday", value=6),
    app_commands.Choice(name="Sunday", value=7),
]

# Module-level group (also needs an explicit tree registration in setup()).
# It is parented to config_group so `/config qotd` works from either import order.
def _build_qotd_group() -> app_commands.Group:
    from cogs.config_cog import config_group

    return app_commands.Group(
        name="qotd",
        description="Question of the Day settings.",
        parent=config_group,
    )


qotd_group = _build_qotd_group()


class QotdCog(commands.Cog, name="QOTD"):
    """Daily question posting with a FIFO queue."""

    def __init__(self, bot: commands.Bot, config_service: ConfigService) -> None:
        self.bot = bot
        self.config = config_service
        self.scheduler: QotdScheduler | None = None

    async def cog_load(self) -> None:
        self.scheduler = QotdScheduler(
            config_service=self.config,
            poster=self._post_question,
        )
        self.scheduler.start()

    async def cog_unload(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.stop()

    # --- posting (called by the scheduler) --------------------------------

    async def _post_question(self, config: GuildConfig, question: dict[str, Any]) -> bool:
        """Deliver a QOTD message. Returns True only on confirmed delivery."""
        guild = self.bot.get_guild(config.guild_id)
        if guild is None:
            log.warning("QOTD: guild %s not available to the bot", config.guild_id)
            return False

        channel_id = config.qotd_channel_id
        if channel_id is None:
            return False

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            log.warning(
                "QOTD: channel %s in guild %s is missing or not messageable",
                channel_id,
                config.guild_id,
            )
            return False

        # Empty-queue notice path.
        if question.get("__empty__"):
            try:
                await channel.send(
                    "I have no questions queued for today. "
                    "An admin can add some with `/config qotd add`."
                )
                return True
            except discord.HTTPException as exc:
                log.error("QOTD empty-queue notice failed: %s", exc)
                return False

        question_text = str(question.get("question") or "").strip()
        if not question_text:
            log.warning("QOTD: question %s has no text", question.get("id"))
            return False

        # Ping the configured role. allowed_mentions keeps it to exactly that.
        ping_role = None
        if config.qotd_role_id:
            ping_role = guild.get_role(config.qotd_role_id)
            if ping_role is None:
                log.warning(
                    "QOTD: role %s not found in guild %s; posting without a ping",
                    config.qotd_role_id,
                    config.guild_id,
                )

        content = f"{ping_role.mention} " if ping_role else ""
        embed = discord.Embed(
            title="Question of the Day",
            description=question_text,
            colour=discord.Colour.blurple(),
        )

        allowed = discord.AllowedMentions(
            roles=[ping_role] if ping_role else [], everyone=False, users=False
        )

        try:
            message = await channel.send(content=content or None, embed=embed, allowed_mentions=allowed)
        except discord.Forbidden:
            log.error(
                "QOTD: missing permissions to post in channel %s of guild %s",
                channel_id,
                config.guild_id,
            )
            return False
        except discord.HTTPException as exc:
            log.error("QOTD: send failed: %s", exc, exc_info=True)
            return False

        # Only now consume the question.
        await discord.utils.maybe_coroutine(
            self.config.qotd_mark_asked,
            config.guild_id,
            int(question["id"]),
            message.id,
        )
        return True

    # --- /config qotd subtree ---------------------------------------------

    @qotd_group.command(name="status", description="Show the current QOTD schedule.")
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        config = self.config.get(interaction.guild_id)
        queue = await discord.utils.maybe_coroutine(self.config.qotd_queue, interaction.guild_id)

        local_time = parse_local_time(config.qotd_time)
        zone = resolve_zone(config.qotd_timezone)

        lines = [
            f"**Enabled:** {'yes' if config.qotd_enabled else 'no'}",
            f"**Time:** {local_time.strftime('%H:%M') if local_time else 'not set'} ({config.qotd_timezone})",
            f"**Channel:** {f'<#{config.qotd_channel_id}>' if config.qotd_channel_id else 'not set'}",
            f"**Ping role:** {f'<@&{config.qotd_role_id}>' if config.qotd_role_id else 'none'}",
            f"**Days:** {_format_days(config.qotd_days)}",
            f"**Questions queued:** {len(queue)}",
        ]

        # Show the next scheduled moment in the guild's own timezone. This is the
        # single most useful line for debugging "why did it post at the wrong hour".
        if local_time is not None:
            upcoming = next_occurrence(
                datetime.now(timezone.utc), local_time, zone, config.qotd_days
            )
            lines.append(
                f"**Next post:** <t:{int(upcoming.timestamp())}:f> "
                f"(<t:{int(upcoming.timestamp())}:R>, {config.qotd_timezone})"
            )

        if queue:
            preview = str(queue[0].get("question") or "")
            if len(preview) > 120:
                preview = preview[:117] + "..."
            lines.append(f"**Next up:** {preview}")

        if self.scheduler is not None and self.scheduler.last_tick is not None:
            lines.append(f"**Scheduler last tick:** <t:{int(self.scheduler.last_tick.timestamp())}:R>")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @qotd_group.command(name="toggle", description="Turn Question of the Day on or off.")
    @app_commands.describe(enabled="Enable or disable QOTD.")
    async def toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_qotd_enabled(gid, enabled),
            f"QOTD is now **{'enabled' if enabled else 'disabled'}**.",
        )

    @qotd_group.command(name="settime", description="Set the daily posting time (24-hour).")
    @app_commands.describe(hour="Hour, 0-23", minute="Minute, 0-59")
    async def settime(
        self, interaction: discord.Interaction, hour: int, minute: int = 0
    ) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_qotd_time(gid, hour, minute),
            f"QOTD time set to **{hour:02d}:{minute:02d}**. "
            f"Remember to check `/config qotd settz` - the default is UTC.",
        )

    @qotd_group.command(name="settz", description="Set the timezone used for the schedule.")
    @app_commands.describe(timezone_name="IANA name, e.g. Asia/Kolkata")
    async def settz(self, interaction: discord.Interaction, timezone_name: str) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_qotd_timezone(gid, timezone_name),
            f"Timezone set to **{timezone_name}**.",
        )

    @qotd_group.command(name="setchannel", description="Set the channel questions post in.")
    async def setchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_qotd_channel(gid, channel.id),
            f"QOTD will post in {channel.mention}.",
        )

    @qotd_group.command(name="setrole", description="Set the role to ping with each question.")
    async def setrole(
        self, interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        role_id = role.id if role else None
        await self._apply(
            interaction,
            lambda gid: self.config.set_qotd_role(gid, role_id),
            f"QOTD ping role set to {role.mention}." if role else "QOTD ping role cleared.",
        )

    @qotd_group.command(name="days", description="Restrict QOTD to specific weekdays.")
    async def days(
        self,
        interaction: discord.Interaction,
        day1: app_commands.Choice[int] | None = None,
        day2: app_commands.Choice[int] | None = None,
        day3: app_commands.Choice[int] | None = None,
        day4: app_commands.Choice[int] | None = None,
        day5: app_commands.Choice[int] | None = None,
        day6: app_commands.Choice[int] | None = None,
        day7: app_commands.Choice[int] | None = None,
    ) -> None:
        selected = sorted(
            {c.value for c in (day1, day2, day3, day4, day5, day6, day7) if c is not None}
        )
        await self._apply(
            interaction,
            lambda gid: self.config.set_qotd_days(gid, selected),
            f"QOTD days set to **{_format_days(selected)}**."
            if selected
            else "QOTD will post **every day**.",
        )

    @qotd_group.command(name="add", description="Add a question to the end of the queue.")
    @app_commands.describe(question="The question to ask.")
    async def add(self, interaction: discord.Interaction, question: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        try:
            row = await discord.utils.maybe_coroutine(
                self.config.qotd_add, interaction.guild_id, question
            )
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        queue = await discord.utils.maybe_coroutine(self.config.qotd_queue, interaction.guild_id)
        await interaction.response.send_message(
            f"Added at position **{row.get('position')}**. Queue length is now {len(queue)}.",
            ephemeral=True,
        )

    @qotd_group.command(name="list", description="List the queued questions.")
    @app_commands.describe(limit="How many to show (default 10).")
    async def list_questions(
        self, interaction: discord.Interaction, limit: int = 10
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        capped = max(1, min(limit, 25))
        queue = await discord.utils.maybe_coroutine(self.config.qotd_queue, interaction.guild_id)

        if not queue:
            await interaction.response.send_message(
                "The queue is empty. Add one with `/config qotd add`.", ephemeral=True
            )
            return

        lines = []
        for index, row in enumerate(queue[:capped], start=1):
            text = str(row.get("question") or "")
            if len(text) > 90:
                text = text[:87] + "..."
            lines.append(f"`{index}.` {text}  (id `{row.get('id')}`)")

        header = f"**{len(queue)}** question(s) queued"
        if len(queue) > capped:
            header += f", showing the first {capped}"
        await interaction.response.send_message(
            header + "\n" + "\n".join(lines), ephemeral=True
        )

    @qotd_group.command(name="remove", description="Remove a question by its queue position.")
    @app_commands.describe(position="Position shown by /config qotd list")
    async def remove(self, interaction: discord.Interaction, position: int) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        queue = await discord.utils.maybe_coroutine(self.config.qotd_queue, interaction.guild_id)
        if position < 1 or position > len(queue):
            await interaction.response.send_message(
                f"Position must be between 1 and {len(queue)}.", ephemeral=True
            )
            return

        row = queue[position - 1]
        await discord.utils.maybe_coroutine(
            self.config.qotd_delete, interaction.guild_id, int(row["id"])
        )
        await interaction.response.send_message("Question removed.", ephemeral=True)

    @qotd_group.command(name="next", description="Preview the next question, or post it now.")
    @app_commands.describe(post_now="Post the next question immediately.")
    async def next_question(
        self, interaction: discord.Interaction, post_now: bool = False
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        config = self.config.get(interaction.guild_id)
        row = await discord.utils.maybe_coroutine(self.config.qotd_pop_next, interaction.guild_id)

        if row is None:
            await interaction.response.send_message("Nothing queued.", ephemeral=True)
            return

        if not post_now:
            await interaction.response.send_message(
                f"Next up: {row.get('question')}\n\nUse `post_now: True` to send it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        delivered = await self._post_question(config, row)
        if delivered:
            await discord.utils.maybe_coroutine(self.config.qotd_mark_fired, interaction.guild_id)
            await interaction.followup.send("Posted.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Could not post - check the channel and my permissions. "
                "The question is still queued.",
                ephemeral=True,
            )

    @qotd_group.command(name="skip", description="Skip (remove) the next queued question.")
    async def skip(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        row = await discord.utils.maybe_coroutine(self.config.qotd_pop_next, interaction.guild_id)
        if row is None:
            await interaction.response.send_message("Nothing queued.", ephemeral=True)
            return

        await discord.utils.maybe_coroutine(
            self.config.qotd_delete, interaction.guild_id, int(row["id"])
        )
        await interaction.response.send_message("Skipped and removed.", ephemeral=True)

    # --- helper -----------------------------------------------------------

    async def _apply(self, interaction: discord.Interaction, operation, success: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        try:
            await discord.utils.maybe_coroutine(operation, interaction.guild_id)
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(success, ephemeral=True)


def _format_days(days: list[int]) -> str:
    if not days:
        return "every day"
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return ", ".join(names[d - 1] for d in days if 1 <= d <= 7)


async def setup(bot: commands.Bot) -> None:
    # `qotd_group` is parented to config_group, so it rides along with the root
    # group that config_cog registers. Only the root gets added to the tree.
    # Guard on the module identity so duplicate registration cannot happen when
    # both cogs are loaded in either order.
    from cogs import config_cog

    root = bot.tree.get_command(config_cog.config_group.name)
    if root is None:
        bot.tree.add_command(config_cog.config_group)
    elif root is not config_cog.config_group:
        log.warning("A different 'config' command is already registered; leaving it alone")

    cog = QotdCog(bot, bot.config_service)  # type: ignore[attr-defined]
    await bot.add_cog(cog)
    # See cogs.bind_group_commands: module-level groups leave binding=None,
    # which makes every subcommand raise CommandSignatureMismatch.
    n = bind_group_commands(cog)
    log.info("Bound %d qotd subcommand(s) to the cog instance", n)
