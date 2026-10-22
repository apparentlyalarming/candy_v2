"""General commands and gateway listeners.

Owns:
  - /hello, /help and the !help prefix equivalent
  - guild join/leave bookkeeping (guilds.bot_present, guilds row upkeep)
  - member join/leave logging
  - the on_message dispatcher that routes prefix commands

The dispatcher lives here because routing is a general concern, not a fun one.

The help menu is ROLE-AWARE: everyone sees the user commands, and the admin
section is shown ONLY to members who can actually run those commands. Discord
already hides /config from people without Manage Server, so documenting it to
them would advertise commands they cannot use.
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

# Brand colour, shared by every help surface.
BRAND_COLOUR = discord.Colour.from_str("#FFB6C1")


def is_admin(member: discord.abc.User | discord.Member | None) -> bool:
    """True when this member may see/run the admin surface.

    Mirrors the gate Discord applies to the /config group (Manage Server) and the
    manual check in _handle_memory_prefix, so the help menu and the commands it
    documents agree on who is an admin. Members have guild_permissions; a User
    (e.g. in a DM) does not, and is never an admin.
    """
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    return bool(perms.manage_guild or perms.administrator)


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
        embed = await self.build_help(interaction.user, interaction.guild_id)
        # Ephemeral: the menu is for the person asking, not for the channel.
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ==================================================================
    # help: one builder, shared by /help and !help
    # ==================================================================

    async def build_help(
        self, member: discord.abc.User | discord.Member | None, guild_id: int | None
    ) -> discord.Embed:
        """Build the role-aware help embed.

        Everyone gets General + Fun. The Admin section is appended ONLY for a
        member who passes :func:`is_admin`, because that is who can run those
        commands. An admin therefore sees a strict superset of a user's menu.
        """
        admin = is_admin(member)

        # The prefix is per-guild (default "!"), so read the live value instead
        # of hardcoding "!". Otherwise every example goes stale the moment an
        # admin runs /config general prefix. In a DM there is no guild row, so
        # the documented default is the only sensible value to show.
        cfg = self.config.get(guild_id) if guild_id is not None else None
        prefix = cfg.prefix if cfg is not None else "!"
        prefix_enabled = cfg.prefix_enabled if cfg is not None else True
        prefix_example = f"`{prefix}candy`"

        if prefix_enabled:
            if cfg is None:
                # DM / no guild row: talk about the default rather than a
                # non-existent "this server".
                prefix_help = (
                    "Prefix commands use `!` by default; each server can set its own."
                )
            else:
                if prefix == "!":
                    prefix_help = "This server uses the default prefix, `!`."
                else:
                    prefix_help = (
                        f"This server's prefix is `{prefix}` - an admin set it."
                    )
                prefix_help += (
                    " Change it with `/config general prefix`."
                    if admin
                    else " An admin can change it with `/config general prefix`."
                )
        else:
            prefix_help = (
                f"Prefix commands are **off** on this server, so `{prefix}` "
                "commands are ignored. Slash commands still work; an admin can "
                "turn text commands on with `/config general prefixcommands`."
            )

        embed = discord.Embed(
            title="🍬 Candy's Command List",
            description=(
                "Here's what I can do."
                if not admin
                else "Here's what I can do. You can see the admin section too."
            ),
            colour=BRAND_COLOUR,
        )

        embed.add_field(
            name="⌨️ Prefix",
            value=prefix_help,
            inline=False,
        )

        embed.add_field(
            name="💬 General",
            value=(
                "`/hello` - say hi to me\n"
                "`/help` - show this menu\n"
                "`/ask` - ask me anything\n"
                f"`@Candy` or {prefix_example} - chat with me\n"
                f"`{prefix}candyp` - same, but I weigh your own history too\n"
                f"`{prefix}help` - this menu as a text command"
            ),
            inline=False,
        )

        # Built from the live action catalog so it never goes stale. Guarded: a
        # catalog read failing must not break the whole menu.
        try:
            actions = await asyncio.to_thread(self.config.list_actions)
        except Exception:  # noqa: BLE001
            actions = []

        if actions:
            names = "  ".join(f"`/{a.get('key')}`" for a in actions)
            embed.add_field(
                name="🎉 Fun & Interaction",
                value=(
                    f"{names}\n\n"
                    "All of these also work as text commands if prefix commands "
                    "are enabled."
                ),
                inline=False,
            )

        if admin:
            embed.add_field(
                name="⚙️ Admin",
                value=(
                    "`/config status` - see this server's setup\n"
                    "`/config general` - prefix and log channel\n"
                    "`/config chat` - AI model, prompt, channels, memory\n"
                    "`/config replies` - customise fun action replies\n"
                    "`/config qotd` - Question of the Day\n"
                    f"`{prefix}memory on|off` - toggle the AI's memory"
                ),
                inline=False,
            )

        embed.set_footer(
            text=(
                "Everything here is also configurable on the dashboard."
                if not admin
                else "Admins can also configure me on the dashboard."
            )
        )
        return embed

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
        # Access is revoked before the log line: leaving (or being kicked/banned)
        # must not leave a dashboard permission row behind. The bot's own gateway
        # is the authority here, not a later browser sync.
        if not member.bot:
            try:
                await asyncio.to_thread(
                    self.config.revoke_dashboard_access, member.guild.id, member.id
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "Failed to revoke dashboard access for %s in %s: %s",
                    member.id,
                    member.guild.id,
                    exc,
                    exc_info=True,
                )

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

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Revoke dashboard access the moment Manage Server is lost.

        Discord fires this for role changes. Computing it from the permission
        objects means role-additions, role-removals and role-permission edits are
        all covered by one comparison.
        """
        if after.bot:
            return

        def manages(member: discord.Member) -> bool:
            perms = member.guild_permissions
            return bool(
                perms.administrator or perms.manage_guild or member.guild.owner_id == member.id
            )

        if not manages(before) or manages(after):
            return

        try:
            revoked = await asyncio.to_thread(
                self.config.revoke_dashboard_access, after.guild.id, after.id
            )
            if revoked:
                log.info(
                    "Revoked dashboard access for %s (%s) in %s: lost Manage Server",
                    after,
                    after.id,
                    after.guild.id,
                )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Failed to revoke dashboard access for %s in %s: %s",
                after.id,
                after.guild.id,
                exc,
                exc_info=True,
            )

    @commands.Cog.listener()
    async def on_guild_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        """A role's permissions changed: every cached row for this guild is suspect."""
        if before.permissions == after.permissions:
            return
        try:
            await asyncio.to_thread(self.config.invalidate_dashboard_access, after.guild.id)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Failed to invalidate dashboard access for %s: %s",
                after.guild.id,
                exc,
                exc_info=True,
            )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        try:
            await asyncio.to_thread(self.config.invalidate_dashboard_access, role.guild.id)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Failed to invalidate dashboard access for %s: %s",
                role.guild.id,
                exc,
                exc_info=True,
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

            # Chat prefix commands. `!candyp` MUST be checked before `!candy`:
            # a plain prefix match would let `!candy` swallow `!candyp` and the
            # personalized path would be unreachable. The word split is what makes
            # the check a boundary test rather than a startswith.
            chat_head = body.split(maxsplit=1)[0] if body else ""
            if chat_head in {"candy", "candyp"}:
                await self._handle_chat_prefix(message, content, body, prefix, chat_head)
                return

            # !memory on / !memory off - admin only, since it changes guild
            # config. Checked BEFORE the hello fallback so it is not shadowed.
            if body.split()[0:1] == ["memory"]:
                await self._handle_memory_prefix(message, body, prefix)
                return

            # !help - the prefix twin of /help. Same builder, so the two surfaces
            # can never drift.
            if body.split()[0:1] == ["help"]:
                embed = await self.build_help(message.author, message.guild.id)
                await message.reply(embed=embed, mention_author=False)
                return

            if body in {"hello", "hi", "hey"}:
                await message.reply(f"Hello, {message.author.mention}! 🍬", mention_author=False)
        except Exception as exc:  # noqa: BLE001
            log.error("on_message dispatcher failed: %s", exc, exc_info=True)

    async def _handle_chat_prefix(
        self,
        message: discord.Message,
        content: str,
        body: str,
        prefix: str,
        head: str,
    ) -> None:
        """Route ``!candy`` and ``!candyp`` to the AI cog.

        The prompt is the ORIGINAL text after the command word (not the lowercased
        ``body``), so the user's casing survives. ``head`` selects the variant:
        ``candyp`` asks the AI cog to add the prompter's own history.

        The prefix is configurable, so the command word is taken from the actual
        message rather than hardcoded. The offset is measured against the
        WHITESPACE-STRIPPED remainder, not the raw message, so "!  candy hi" (extra
        spaces after the prefix) still yields the prompt "hi" rather than a mangled
        slice.
        """
        ai_cog = self.bot.get_cog("AI")
        if ai_cog is None:
            return

        # Strip "<prefix><head>" from the original text, leaving the prompt. Work
        # from the position of the command word in the raw content so the user's
        # casing is preserved.
        after_prefix = content[len(prefix):]
        stripped = after_prefix.lstrip()
        prompt = stripped[len(head):].strip()

        personalized = head == "candyp"
        handled = await ai_cog.handle_chat(message, prompt, personalized=personalized)  # type: ignore[attr-defined]
        if not handled:
            # The AI declined (disabled, or channel not allowed). Say so instead of
            # looking broken - silence here reads as "!candy does nothing".
            config = self.config.get(message.guild.id) if message.guild else None
            if config is not None and not config.ai_enabled:
                await message.reply(
                    "AI chat is disabled here. An admin can enable it with "
                    "`/config chat toggle`.",
                    mention_author=False,
                )
            elif config is not None and not config.ai_allowed_in(message.channel.id):
                await message.reply(
                    "I'm not set up to chat in this channel. An admin can add it "
                    "with `/config chat channels`.",
                    mention_author=False,
                )

    async def _handle_memory_prefix(
        self, message: discord.Message, body: str, prefix: str
    ) -> None:
        """Handle ``<prefix>memory <on|off>``. Admin only.

        Config changes are Manage Server everywhere else (every /config
        subcommand), so a prefix shortcut to the same setting must not be
        open to everyone - otherwise the prefix path would be a permission
        bypass. Reported ephemerally-ish (a normal reply) because a silent
        refusal looks like the command is broken.
        """
        if message.guild is None:
            return

        # Honour the role hierarchy the same way slash commands do.
        member = message.author
        perms = getattr(member, "guild_permissions", None)
        if perms is None or not (perms.manage_guild or perms.administrator):
            await message.reply(
                "You need **Manage Server** to change that.", mention_author=False
            )
            return

        parts = body.split()
        if len(parts) != 2 or parts[1] not in {"on", "off", "true", "false", "yes", "no"}:
            await message.reply(
                f"Usage: `{prefix}memory on` or `{prefix}memory off`",
                mention_author=False,
            )
            return

        enabled = parts[1] in {"on", "true", "yes"}

        try:
            await asyncio.to_thread(self.config.set_ai_memory_enabled, message.guild.id, enabled)
        except Exception as exc:  # noqa: BLE001
            log.error("Prefix memory toggle failed: %s", exc, exc_info=True)
            await message.reply(
                "Could not save that. Check the bot logs.", mention_author=False
            )
            return

        if enabled:
            note = "I'll recall earlier messages again."
        else:
            note = "I'll forget from here on - nothing already stored was deleted."

        await message.reply(
            f"Memory is now **{'on' if enabled else 'off'}**. {note}",
            mention_author=False,
        )

        emitter = getattr(self.bot, "emit_log", None)
        if emitter is not None:
            await emitter(
                message.guild.id,
                "config",
                title=f"{prefix}memory {'on' if enabled else 'off'}",
                description=f"{message.author.mention} turned AI memory {'on' if enabled else 'off'}.",
                actor_id=message.author.id,
                actor_name=str(message.author),
                channel_id=message.channel.id,
                metadata={"source": "prefix memory"},
            )

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
