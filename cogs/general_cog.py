"""General commands and gateway listeners.

Owns:
  - /hello, /help
  - guild join/leave bookkeeping (guilds.bot_present, guilds row upkeep)
  - member join/leave logging
  - the on_message dispatcher that routes prefix commands

The dispatcher lives here because routing is a general concern, not a fun one.
"""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from services.config_service import ConfigService
from utils.logging_setup import get_logger

log = get_logger("cogs.general")

# Acknowledge to Discord quickly during startup sync.
SYNC_TIMEOUT_SECONDS = 60


class GeneralCog(commands.Cog, name="General"):
    """Everyday commands and gateway event handling."""

    def __init__(self, bot: commands.Bot, config_service: ConfigService) -> None:
        self.bot = bot
        self.config = config_service

    # ==================================================================
    # slash commands
    # ==================================================================

    @app_commands.command(name="hello", description="Say hi to Candy.")
    async def hello(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(f"Hello, {interaction.user.mention}! 🍬")

    @app_commands.command(name="help", description="Show everything Candy can do.")
    async def help_command(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🍬 Candy's Command List",
            description="Here's everything I can do.",
            colour=discord.Colour.from_str("#FFB6C1"),
        )

        embed.add_field(
            name="💬 General",
            value="`/hello` - say hi to me\n`/help` - show this menu\n`/ask` - ask me anything",
            inline=False,
        )

        # Built from the live action catalog so it never goes stale.
        try:
            actions = await asyncio.to_thread(self.config.list_actions)
        except Exception:
            actions = []

        if actions:
            names = "  ".join(f"`/{a.get('key')}`" for a in actions)
            embed.add_field(
                name="🎉 Fun & Interaction",
                value=f"{names}\n\nAll of these also work as text commands if prefix commands are enabled.",
                inline=False,
            )

        embed.add_field(
            name="⚙️ Admin",
            value=(
                "`/config status` - see this server's setup\n"
                "`/config chat` - AI model, prompt, channels\n"
                "`/config replies` - customise fun action replies\n"
                "`/config qotd` - Question of the Day\n"
                "`/config general` - prefix and log channel"
            ),
            inline=False,
        )

        embed.set_footer(text="Everything here is also configurable on the dashboard.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ==================================================================
    # gateway events
    # ==================================================================

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Joined guild %s (%s)", guild.id, guild.name)
        try:
            await asyncio.to_thread(
                self.config.ensure_guild_row,
                guild.id,
                name=guild.name,
                icon_url=str(guild.icon.url) if guild.icon else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to register guild %s: %s", guild.id, exc, exc_info=True)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Removed from guild %s (%s)", guild.id, guild.name)
        try:
            await asyncio.to_thread(self.config.mark_bot_absent, guild.id)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to mark guild %s absent: %s", guild.id, exc, exc_info=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        emitter = getattr(self.bot, "emit_log", None)
        if emitter is None:
            return
        await emitter(
            member.guild.id,
            "members",
            title="Member joined",
            description=f"{member.mention} ({member}) joined the server.",
            actor_id=member.id,
            actor_name=str(member),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        emitter = getattr(self.bot, "emit_log", None)
        if emitter is None:
            return
        await emitter(
            member.guild.id,
            "members",
            title="Member left",
            description=f"{member} ({member.id}) left the server.",
            actor_id=member.id,
            actor_name=str(member),
        )

    # ==================================================================
    # prefix dispatcher
    # ==================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Route prefix commands. Fails closed: any error is swallowed and logged.

        Order of checks matters for cost. Bots and DMs are rejected before any
        parsing, which is the single biggest saving on a Memory-constrained host.
        """
        try:
            # 1. Never respond to bots (prevents bot-to-bot loops).
            if message.author.bot:
                return
            # 2. Ignore DMs and system messages.
            if message.guild is None or message.type is not discord.MessageType.default:
                return

            # 3. Mentions get an AI reply when the channel is allowed. Checked
            #    before the prefix so "@Candy hi" works with no prefix at all.
            cleaned = self._strip_mention(message)
            if cleaned is not None:
                ai_cog = self.bot.get_cog("AI")
                if ai_cog is not None:
                    handled = await ai_cog.handle_mention(message, cleaned)  # type: ignore[attr-defined]
                    if handled:
                        return

            config = self.config.get(message.guild.id)

            # 4. Prefix commands are opt-out per guild.
            if not config.prefix_enabled:
                return

            content = message.content or ""
            prefix = config.prefix
            if not content.startswith(prefix):
                return

            fun_cog = self.bot.get_cog("Fun")
            if fun_cog is not None:
                handled = await fun_cog.handle_prefix(message, content, prefix)  # type: ignore[attr-defined]
                if handled:
                    return

            body = content[len(prefix):].strip().lower()
            if body in {"hello", "hi", "hey"}:
                await message.reply(f"Hello, {message.author.mention}! 🍬", mention_author=False)
        except Exception as exc:  # noqa: BLE001
            log.error("on_message dispatcher failed: %s", exc, exc_info=True)

    def _strip_mention(self, message: discord.Message) -> str | None:
        """Return the prompt if this message mentions the bot, else None.

        An empty mention ("@Candy" with nothing else) returns an empty string,
        which callers treat as "still a mention, but with no prompt".
        Returns None when the bot is not mentioned at all.
        """
        me = self.bot.user
        if me is None or not message.mentions:
            return None
        if not any(user.id == me.id for user in message.mentions):
            return None

        content = message.content or ""
        # Strip both the plain <@id> and the nickname <@!id> forms, then collapse
        # the double spaces the removal leaves behind ("hey  <@1>  there").
        for token in (f"<@{me.id}>", f"<@!{me.id}>"):
            content = content.replace(token, " ")
        return " ".join(content.split()).strip()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GeneralCog(bot, bot.config_service))  # type: ignore[attr-defined]
