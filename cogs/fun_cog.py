"""Fun action commands: /hug, /kick, /pat, ... plus prefix aliases.

Design notes:
  - Reply templates come from ConfigService.resolve_templates(), which applies
    the 3-step fallback (guild override -> shipped default -> suppress). The web
    dashboard implements the same order in its preview pane.
  - Slash commands are generated from the reply_actions catalog at cog load, so
    adding an action to the DB needs no code change.
  - Prefix aliases (``!hug``) dispatch into the SAME run_fun_action() the slash
    command uses. One implementation, two front doors.
"""

from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from services.config_service import ConfigService
from services.render import TemplateError, render_random
from utils.logging_setup import get_logger

log = get_logger("cogs.fun")

# Discord permission bit for MANAGE_GUILD.
MANAGE_GUILD = 32

# Cap on auto-generated slash commands, so a runaway catalog cannot blow past
# Discord's 100-command limit.
MAX_GENERATED_COMMANDS = 60


class FunCog(commands.Cog, name="Fun"):
    """Randomised action replies, driven by the reply_actions catalog."""

    def __init__(self, bot: commands.Bot, config_service: ConfigService) -> None:
        self.bot = bot
        self.config = config_service
        self._actions: list[dict[str, Any]] = []
        self._action_map: dict[str, dict[str, Any]] = {}
        self._registered: list[str] = []

    # --- lifecycle ---------------------------------------------------------

    async def cog_load(self) -> None:
        """Load the action catalog and register one slash command per action."""
        try:
            self._actions = await asyncio.to_thread(self.config.list_actions)
        except Exception as exc:
            log.error("Could not load reply_actions catalog: %s", exc, exc_info=True)
            self._actions = []

        for action in self._actions:
            key = str(action.get("key") or "").strip()
            if key:
                self._action_map[key] = action

        self._register_all_commands()

        log.info(
            "Fun cog loaded with %d actions: %s",
            len(self._action_map),
            ", ".join(sorted(self._action_map)),
        )

    async def cog_unload(self) -> None:
        for name in self._registered:
            self.bot.tree.remove_command(name)

    def _register_all_commands(self) -> None:
        """Build one app command per catalog action and add it to the tree.

        Commands are built with app_commands.Command directly (rather than the
        decorator) because the set is data-driven and generated at runtime.
        """
        created = 0
        for key, action in sorted(self._action_map.items()):
            if created >= MAX_GENERATED_COMMANDS:
                log.warning(
                    "Stopped generating fun commands at %d; catalog is larger than expected",
                    MAX_GENERATED_COMMANDS,
                )
                break

            label = str(action.get("label") or key.title())
            callback = self._make_callback(key)

            command = app_commands.Command(
                name=key,
                description=f"{label} someone.",
                callback=callback,
            )
            command.parameters = [
                app_commands.Parameter(
                    name="user",
                    description="Who to direct this at.",
                    required=True,
                    type=discord.AppCommandOptionType.user,
                )
            ]

            if self.bot.tree.get_command(key) is not None:
                self.bot.tree.remove_command(key)

            self.bot.tree.add_command(command)
            self._registered.append(key)
            created += 1

    def _make_callback(self, action_key: str):
        async def _callback(interaction: discord.Interaction, user: discord.User) -> None:
            await self._handle_slash(interaction, action_key, user)

        return _callback

    # --- shared action runner ---------------------------------------------

    async def run_fun_action(
        self,
        *,
        guild_id: int,
        author_id: int,
        target_id: int,
        action: str,
    ) -> str | None:
        """Return the rendered reply text, or None when it should be suppressed.

        THE single code path for both slash and prefix invocations.
        """
        if self._action_map and action not in self._action_map:
            return None

        try:
            templates = await asyncio.to_thread(
                self.config.resolve_templates, guild_id, action
            )
        except Exception as exc:
            log.error("Could not resolve templates for %s: %s", action, exc, exc_info=True)
            return None

        if not templates:
            log.debug(
                "No templates for action %s in guild %s; suppressing reply", action, guild_id
            )
            return None

        try:
            return render_random(templates, author_id, target_id)
        except TemplateError as exc:
            log.error("Template render failed for %s in guild %s: %s", action, guild_id, exc)
            return None

    async def _handle_slash(
        self, interaction: discord.Interaction, action: str, target: discord.User
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Fun actions only work inside a server.", ephemeral=True
            )
            return

        if target.id == interaction.user.id:
            await interaction.response.send_message(
                _self_target_message(action), ephemeral=True
            )
            return

        await interaction.response.defer(thinking=False)

        text = await self.run_fun_action(
            guild_id=interaction.guild_id,
            author_id=interaction.user.id,
            target_id=target.id,
            action=action,
        )

        if text is None:
            await interaction.followup.send(
                f"The **{action}** reply list is empty or disabled here. "
                "An admin can set it up with `/config replies`.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(text)
        await self._log_usage(interaction, action, target.id)

    async def _log_usage(
        self, interaction: discord.Interaction, action: str, target_id: int
    ) -> None:
        emitter = getattr(self.bot, "emit_log", None)
        if emitter is None:
            return
        await emitter(
            interaction.guild_id,
            "commands",
            title=f"/{action}",
            description=f"{interaction.user.mention} used `{action}` on <@{target_id}>.",
            channel_id=interaction.channel_id,
            actor_id=interaction.user.id,
        )

    # --- prefix aliases ----------------------------------------------------

    async def handle_prefix(
        self, message: discord.Message, content: str, prefix: str
    ) -> bool:
        """Handle ``<prefix><action> <mention>``. Returns True when consumed."""
        if not content.startswith(prefix):
            return False

        body = content[len(prefix):].strip()
        if not body:
            return False

        parts = body.split(maxsplit=1)
        action = parts[0].lower()
        if action not in self._action_map:
            return False

        if message.guild is None:
            return True

        target = await self._resolve_target(message, parts[1] if len(parts) > 1 else "")
        if target is None:
            await message.reply(f"Usage: `{prefix}{action} @user`", mention_author=False)
            return True

        if target.id == message.author.id:
            await message.reply(_self_target_message(action), mention_author=False)
            return True

        text = await self.run_fun_action(
            guild_id=message.guild.id,
            author_id=message.author.id,
            target_id=target.id,
            action=action,
        )

        if text is None:
            await message.reply(
                f"The **{action}** reply list is empty or disabled here.",
                mention_author=False,
            )
            return True

        await message.reply(text, mention_author=False)

        emitter = getattr(self.bot, "emit_log", None)
        if emitter is not None:
            await emitter(
                message.guild.id,
                "commands",
                title=f"{prefix}{action}",
                description=f"{message.author.mention} used `{action}` on {target.mention}.",
                channel_id=message.channel.id,
                actor_id=message.author.id,
            )
        return True

    async def _resolve_target(
        self, message: discord.Message, raw: str
    ) -> discord.User | discord.Member | None:
        if message.mentions:
            return message.mentions[0]

        token = raw.strip().strip("<@!>")
        if token.isdigit():
            if message.guild:
                member = message.guild.get_member(int(token))
                if member is not None:
                    return member
            try:
                return await self.bot.fetch_user(int(token))
            except (discord.NotFound, discord.HTTPException):
                return None

        if raw.strip() and message.guild:
            return message.guild.get_member_named(raw.strip())
        return None


def _self_target_message(action: str) -> str:
    """Explicit self-target behaviour: tease rather than silently block."""
    return f"You can't `{action}` yourself. Find a friend instead."


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FunCog(bot, bot.config_service))  # type: ignore[attr-defined]
