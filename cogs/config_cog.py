"""The /config hub: the parity surface between slash commands and the dashboard.

Every setting the web app can edit has a subcommand here, and both write the same
Supabase columns. Source of truth is always Supabase - slash commands write rows,
the cache updates via Realtime, and the web writes rows directly. No two-way sync,
no conflict rules.

Subcommand groups:
  /config status          overview
  /config general         prefix, prefix commands, log channel
  /config chat            AI model, prompt, channels, context, cooldown
  /config replies         fun action reply templates
  /config qotd            (registered by qotd_cog)

IMPLEMENTATION NOTE: the command groups are defined at MODULE level and named
`config_group` / `general_group` / etc. A class attribute called `config` would
shadow `self.config` (the ConfigService) for every method in the class and
silently break every write. Module-level Group objects avoid that entirely and
are the idiomatic discord.py pattern.
"""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from services.config_service import ConfigError, ConfigService
from services.render import preview
from utils.logging_setup import get_logger

log = get_logger("cogs.config")

MANAGE_GUILD_PERMS = discord.Permissions(manage_guild=True)

# Suggested models. Groq retires models over time, so free text is also allowed.
MODEL_CHOICES = [
    app_commands.Choice(name="llama-3.3-70b-versatile", value="llama-3.3-70b-versatile"),
    app_commands.Choice(name="llama-3.1-8b-instant", value="llama-3.1-8b-instant"),
    app_commands.Choice(name="openai/gpt-oss-120b", value="openai/gpt-oss-120b"),
    app_commands.Choice(name="openai/gpt-oss-20b", value="openai/gpt-oss-20b"),
]

EVENT_CATEGORIES = [
    app_commands.Choice(name="config changes", value="config"),
    app_commands.Choice(name="command usage", value="commands"),
    app_commands.Choice(name="QOTD activity", value="qotd"),
    app_commands.Choice(name="errors", value="errors"),
    app_commands.Choice(name="member join/leave", value="members"),
    app_commands.Choice(name="AI activity", value="ai"),
]

# --- command groups (module level: no shadowing) ----------------------------

config_group = app_commands.Group(
    name="config",
    description="Configure Candy for this server.",
    default_permissions=MANAGE_GUILD_PERMS,
)

general_group = app_commands.Group(
    name="general", description="General bot settings.", parent=config_group
)

chat_group = app_commands.Group(
    name="chat", description="Chat AI settings.", parent=config_group
)

replies_group = app_commands.Group(
    name="replies", description="Fun action reply templates.", parent=config_group
)

dev_group = app_commands.Group(
    name="dev", description="Bot-owner only: host maintenance.", parent=config_group
)


async def _to_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


class ConfigCog(commands.Cog, name="Config"):
    """Administrative configuration."""

    def __init__(self, bot: commands.Bot, config_service: ConfigService) -> None:
        self.bot = bot
        self.config = config_service

    # ==================================================================
    # /config status
    # ==================================================================

    @config_group.command(name="status", description="Show an overview of this server's settings.")
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        cfg = self.config.get(interaction.guild_id)
        actions = await _to_thread(self.config.list_actions)
        queue = await _to_thread(self.config.qotd_queue, interaction.guild_id)

        ai_channels = cfg.ai_channel_ids
        if cfg.ai_all_channels:
            channels_text = "all channels"
        elif ai_channels:
            channels_text = ", ".join(f"<#{c}>" for c in ai_channels[:5])
            if len(ai_channels) > 5:
                channels_text += f" (+{len(ai_channels) - 5} more)"
        else:
            channels_text = "none (AI will not reply)"

        lines = [
            "**General**",
            f"Prefix: `{cfg.prefix}`  •  prefix commands: {'on' if cfg.prefix_enabled else 'off'}",
            f"Log channel: {f'<#{cfg.log_channel_id}>' if cfg.log_channel_id else 'not set'}"
            f"  •  categories: {', '.join(cfg.log_categories) or 'none'}",
            "",
            "**Chat AI**",
            f"Enabled: {'yes' if cfg.ai_enabled else 'no'}  •  model: `{cfg.ai_model}`",
            f"Channels: {channels_text}",
            f"Temperature: {cfg.ai_temperature}  •  context: {cfg.ai_context_messages} messages",
            f"Memory: {'on' if cfg.ai_memory_enabled else 'off'}"
            + (
                f"  •  window: {cfg.ai_memory_days or 'forever'} day(s)"
                if cfg.ai_memory_enabled
                else " (nothing read or saved)"
            ),
            f"Memory ignored in: {', '.join(f'<#{c}>' for c in cfg.ai_memory_ignored_channels) or 'no channels'}",
            f"Cooldown: {cfg.ai_user_cooldown_seconds}s per user",
            "",
            "**Question of the Day**",
            f"Enabled: {'yes' if cfg.qotd_enabled else 'no'}  •  queued: {len(queue)}",
            "",
            f"**Fun actions:** {len(actions)} available",
            "",
            "Everything here is also editable on the dashboard.",
        ]

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ==================================================================
    # /config general
    # ==================================================================

    @general_group.command(name="prefix", description="Set the text command prefix.")
    @app_commands.describe(prefix="Up to 5 characters, e.g. ! or ?")
    async def set_prefix(self, interaction: discord.Interaction, prefix: str) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_prefix(gid, prefix),
            f"Prefix set to `{prefix}`.",
        )

    @general_group.command(
        name="prefixcommands", description="Enable or disable text commands like !hug."
    )
    @app_commands.describe(enabled="Text commands need the Message Content intent.")
    async def prefix_commands(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_prefix_enabled(gid, enabled),
            f"Prefix commands are now **{'enabled' if enabled else 'disabled'}**.",
        )

    @general_group.command(
        name="logchannel", description="Set (or clear) the channel for bot log messages."
    )
    @app_commands.describe(channel="Leave empty to disable logging.")
    async def log_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        if channel is not None:
            me = interaction.guild.me if interaction.guild else None
            if me is not None:
                perms = channel.permissions_for(me)
                if not (perms.send_messages and perms.embed_links):
                    await interaction.response.send_message(
                        f"I need **Send Messages** and **Embed Links** in {channel.mention} "
                        "before I can log there.",
                        ephemeral=True,
                    )
                    return

        channel_id = channel.id if channel else None
        await self._apply(
            interaction,
            lambda gid: self.config.set_log_channel(gid, channel_id),
            (
                f"Log channel set to {channel.mention}. "
                f"Choose what gets logged with `/config general logevents`."
            )
            if channel
            else "Log channel cleared - nothing will be logged.",
        )

    @general_group.command(
        name="logevents", description="Choose which event categories go to the log channel."
    )
    @app_commands.describe(category="Toggle this category on or off.")
    @app_commands.choices(category=EVENT_CATEGORIES)
    async def log_events(
        self, interaction: discord.Interaction, category: app_commands.Choice[str]
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        cfg = self.config.get(interaction.guild_id)
        current = list(cfg.log_categories)
        value = category.value

        if value in current:
            current.remove(value)
            verb = "disabled"
        else:
            current.append(value)
            verb = "enabled"

        try:
            await _to_thread(self.config.set_log_categories, interaction.guild_id, current)
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"**{category.name}** logging {verb}. "
            f"Active categories: {', '.join(sorted(current)) or 'none'}.",
            ephemeral=True,
        )

    @general_group.command(
        name="logtoggle", description="Turn all logging to the log channel on or off."
    )
    async def log_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_log_enabled(gid, enabled),
            f"Logging is now **{'enabled' if enabled else 'disabled'}**.",
        )

    # ==================================================================
    # /config chat
    # ==================================================================

    @chat_group.command(name="toggle", description="Turn AI chat on or off.")
    async def chat_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        suffix = " Set channels with `/config chat channels`." if enabled else ""
        await self._apply(
            interaction,
            lambda gid: self.config.set_ai_enabled(gid, enabled),
            f"AI chat is now **{'enabled' if enabled else 'disabled'}**.{suffix}",
        )

    @chat_group.command(name="memory", description="Let the AI remember this server (on), or forget (off).")
    @app_commands.describe(
        enabled="On: the AI recalls past chat. Off: it reads and saves nothing."
    )
    async def chat_memory(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        # Note for the operator: turning memory off does NOT delete what was
        # already stored, so the user is told it is reversible rather than left
        # to assume their history was erased.
        if enabled:
            note = (
                "The AI will recall earlier messages again. "
                "Previously stored history was kept, so that context is back."
            )
        else:
            note = (
                "The AI will stop recalling past chat **and** stop saving new "
                "messages. Nothing already stored was deleted, so turning it "
                "back on restores the previous context."
            )
        await self._apply(
            interaction,
            lambda gid: self.config.set_ai_memory_enabled(gid, enabled),
            f"Memory is now **{'on' if enabled else 'off'}**. {note}",
        )

    @chat_group.command(
        name="memorydays",
        description="How many days of chat the AI remembers. 0 = forever.",
    )
    @app_commands.describe(days="0-3650. Each message expires on its own Nth day; nothing is ever wiped wholesale.")
    async def chat_memory_days(self, interaction: discord.Interaction, days: int) -> None:
        if days == 0:
            note = "The AI will keep its memory indefinitely."
        else:
            note = (
                f"Messages older than **{days} days** are deleted as new ones "
                "arrive. Each one expires on its own day - the whole history is "
                "never cleared at once."
            )
        await self._apply(
            interaction,
            lambda gid: self.config.set_ai_memory_days(gid, days),
            f"Memory window set to **{days}** day(s). {note}",
        )

    @chat_group.command(
        name="memorychannels",
        description="Channels the AI must never remember or recall.",
    )
    @app_commands.describe(
        channel="Channel to add or remove.",
        action="Defaults to add.",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="add (stop remembering here)", value="add"),
            app_commands.Choice(name="remove (remember here again)", value="remove"),
            app_commands.Choice(name="clear", value="clear"),
        ]
    )
    async def chat_memory_channels(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        if action.value in {"add", "remove"} and channel is None:
            await interaction.response.send_message(
                f"Pick a channel to `{action.value}`.", ephemeral=True
            )
            return

        gid = interaction.guild_id
        try:
            if action.value == "add":
                await _to_thread(self.config.add_ai_memory_ignored_channel, gid, channel.id)
                message = (
                    f"{channel.mention} is now **ignored**: nothing said there is "
                    "remembered or recalled."
                )
            elif action.value == "remove":
                await _to_thread(self.config.remove_ai_memory_ignored_channel, gid, channel.id)
                message = f"{channel.mention} is remembered again."
            else:
                await _to_thread(self.config.set_ai_memory_ignored_channels, gid, [])
                message = "Ignored-channel list cleared - every channel is remembered."
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(message, ephemeral=True)

    @chat_group.command(name="model", description="Choose the AI model.")
    @app_commands.choices(model=MODEL_CHOICES)
    async def chat_model(
        self, interaction: discord.Interaction, model: app_commands.Choice[str]
    ) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_ai_model(gid, model.value),
            f"Model set to `{model.value}`.",
        )

    @chat_group.command(name="prompt", description="Set the AI system prompt (its personality).")
    @app_commands.describe(prompt="Up to 4000 characters.")
    async def chat_prompt(self, interaction: discord.Interaction, prompt: str) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_ai_system_prompt(gid, prompt),
            "System prompt updated.",
        )

    @chat_group.command(
        name="channels", description="Add, remove, or clear the AI response channels."
    )
    @app_commands.describe(channel="Channel to add or remove.", action="Defaults to add.")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="add", value="add"),
            app_commands.Choice(name="remove", value="remove"),
            app_commands.Choice(name="clear", value="clear"),
            app_commands.Choice(name="all channels", value="all"),
        ]
    )
    async def chat_channels(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        if action.value in {"add", "remove"} and channel is None:
            await interaction.response.send_message(
                f"Pick a channel to `{action.value}`.", ephemeral=True
            )
            return

        gid = interaction.guild_id
        try:
            if action.value == "add":
                await _to_thread(self.config.add_ai_channel, gid, channel.id)
                message = f"Added {channel.mention}. The AI now also replies there."
            elif action.value == "remove":
                await _to_thread(self.config.remove_ai_channel, gid, channel.id)
                message = f"Removed {channel.mention}."
            elif action.value == "clear":
                await _to_thread(self.config.set_ai_channels, gid, [])
                message = "Channel list cleared. The AI will not reply anywhere."
            else:
                await _to_thread(self.config.set_ai_all_channels, gid, True)
                message = "The AI will reply in **every** channel where it is mentioned."
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        # Choosing an explicit channel implies "not all channels".
        if action.value != "all" and self.config.get(gid).ai_all_channels:
            await _to_thread(self.config.set_ai_all_channels, gid, False)
            message += " (all-channels mode turned off)"

        await interaction.response.send_message(message, ephemeral=True)

    @chat_group.command(name="context", description="How many past messages the AI sees.")
    @app_commands.describe(messages="0-50. 0 treats each message as fresh.")
    async def chat_context(self, interaction: discord.Interaction, messages: int) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_ai_context_messages(gid, messages),
            f"The AI will consider the last **{messages}** message(s) of context.",
        )

    @chat_group.command(name="cooldown", description="Per-user seconds between AI requests.")
    @app_commands.describe(seconds="0-3600")
    async def chat_cooldown(self, interaction: discord.Interaction, seconds: int) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_ai_cooldown(gid, seconds),
            f"Cooldown set to **{seconds}s** per user.",
        )

    @chat_group.command(name="temperature", description="How creative the AI is (0.0-2.0).")
    async def chat_temperature(self, interaction: discord.Interaction, value: float) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_ai_temperature(gid, value),
            f"Temperature set to **{value}**.",
        )

    @chat_group.command(name="maxtokens", description="Maximum length of an AI reply.")
    async def chat_max_tokens(self, interaction: discord.Interaction, tokens: int) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_ai_max_tokens(gid, tokens),
            f"Max tokens set to **{tokens}**.",
        )

    # ==================================================================
    # /config replies
    # ==================================================================

    @replies_group.command(name="list", description="List reply templates for an action.")
    @app_commands.describe(action="Which action, e.g. hug")
    async def replies_list(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        key = action.lower().strip()
        catalog = await _to_thread(self.config.get_action, key)
        if catalog is None:
            await interaction.response.send_message(
                f"Unknown action `{key}`. Try `/config replies actions`.", ephemeral=True
            )
            return

        templates = await _to_thread(self.config.resolve_templates, interaction.guild_id, key)
        state = await _to_thread(self.config.reply_override_state, interaction.guild_id, key)

        if not templates:
            await interaction.response.send_message(
                f"`{key}` has no templates and would be suppressed. "
                "Add one with `/config replies add`.",
                ephemeral=True,
            )
            return

        lines = [f"**{key}** ({state}) - {len(templates)} template(s):"]
        for index, text in enumerate(preview(templates), start=1):
            lines.append(f"`{index}.` {text}")
        lines.append("\nEdit these on the dashboard, or with `/config replies add` / `remove`.")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @replies_group.command(name="actions", description="List every available fun action.")
    async def replies_actions(self, interaction: discord.Interaction) -> None:
        actions = await _to_thread(self.config.list_actions)
        if not actions:
            await interaction.response.send_message(
                "No actions configured. Has `schema.sql` been run?", ephemeral=True
            )
            return

        lines = [
            f"{a.get('emoji') or ''} `{a.get('key')}` - {a.get('label')} "
            f"({len(a.get('default_templates') or [])} default replies)"
            for a in actions
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @replies_group.command(name="add", description="Add a reply template to an action.")
    @app_commands.describe(
        action="Which action, e.g. hug",
        template="Use {user} and {target}. Example: {user} hugs {target} tightly",
    )
    async def replies_add(
        self, interaction: discord.Interaction, action: str, template: str
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        try:
            await _to_thread(
                self.config.add_reply_template,
                interaction.guild_id,
                action.lower().strip(),
                template,
            )
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        rendered = preview([template], "You", "Them")[0]
        await interaction.response.send_message(
            f"Added a `{action}` reply.\nPreview: {rendered}", ephemeral=True
        )

    @replies_group.command(name="remove", description="Remove a reply template by index.")
    @app_commands.describe(index="The number shown by /config replies list")
    async def replies_remove(
        self, interaction: discord.Interaction, action: str, index: int
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        try:
            removed = await _to_thread(
                self.config.remove_reply_template,
                interaction.guild_id,
                action.lower().strip(),
                index,
            )
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(f"Removed: {removed}", ephemeral=True)

    @replies_group.command(name="toggle", description="Enable or disable an action.")
    async def replies_toggle(
        self, interaction: discord.Interaction, action: str, enabled: bool
    ) -> None:
        await self._apply(
            interaction,
            lambda gid: self.config.set_reply_enabled(gid, action.lower().strip(), enabled),
            f"`{action}` replies are now **{'enabled' if enabled else 'disabled'}**.",
        )

    @replies_group.command(name="reset", description="Reset an action to the shipped defaults.")
    async def replies_reset(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        try:
            removed = await _to_thread(
                self.config.reset_reply_templates,
                interaction.guild_id,
                action.lower().strip(),
            )
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        note = "It now uses the default replies." if removed else "It was already on defaults."
        await interaction.response.send_message(f"Reset `{action}`. {note}", ephemeral=True)

    # ==================================================================
    # /config dev
    # ==================================================================

    async def _is_bot_owner(self, interaction: discord.Interaction) -> bool:
        """True when the caller owns the bot application.

        The host renewal is account-level, not per-guild, so Manage Server is the
        wrong gate. Delegates to the reminder's cached lookup when it exists, so
        the button and this command cannot disagree about who the owner is.
        """
        reminder = getattr(interaction.client, "rotation_reminder", None)
        if reminder is not None:
            return await reminder.is_owner(interaction.user.id)

        try:
            info = await interaction.client.application_info()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not check bot ownership: %s", exc)
            return False

        if info.team is not None:
            return any(m.id == interaction.user.id for m in info.team.members)
        return info.owner is not None and info.owner.id == interaction.user.id

    @dev_group.command(
        name="renewed",
        description="Bot owner: reset the host-renewal reminder after renewing.",
    )
    async def dev_renewed(self, interaction: discord.Interaction) -> None:
        if not await self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "That command is for the bot owner only.", ephemeral=True
            )
            return

        reminder = getattr(interaction.client, "rotation_reminder", None)
        if reminder is None:
            await interaction.response.send_message(
                "The renewal reminder is disabled "
                "(`ROTATION_REMINDER_DAYS=0`).",
                ephemeral=True,
            )
            return

        moment = await _to_thread(reminder.mark_renewed)
        days = reminder.threshold_days

        if interaction.guild_id is not None:
            emit = getattr(interaction.client, "emit_log", None)
            if emit is not None:
                await emit(
                    interaction.guild_id,
                    "config",
                    title="Host renewal recorded",
                    description=f"{interaction.user} marked the host as renewed.",
                    actor_id=interaction.user.id,
                    actor_name=str(interaction.user),
                    metadata={"source": "config dev renewed"},
                )

        await interaction.response.send_message(
            f"Renewal recorded at {moment:%Y-%m-%d %H:%M} UTC. "
            f"The reminder will next fire {days:.0f} day(s) from now.",
            ephemeral=True,
        )

    # ==================================================================
    # helper
    # ==================================================================

    async def _apply(self, interaction: discord.Interaction, operation, success: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        try:
            await _to_thread(operation, interaction.guild_id)
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 - surface a clean message, log the detail
            log.error("Config write failed: %s", exc, exc_info=True)
            await interaction.response.send_message(
                "Something went wrong saving that. Check the bot logs.", ephemeral=True
            )
            return
        await interaction.response.send_message(success, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    # Module-level groups are NOT auto-registered the way decorators inside a
    # Cog are, so the tree has to be given the root group explicitly.
    if bot.tree.get_command(config_group.name) is None:
        bot.tree.add_command(config_group)
    await bot.add_cog(ConfigCog(bot, bot.config_service))  # type: ignore[attr-defined]
