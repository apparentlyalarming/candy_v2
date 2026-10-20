"""AI chat: /ask, mention handling, and the fallback tells the user why.

The Groq key lives in Supabase secrets behind the `chat` Edge Function, so this
cog never sees a provider key. If the function is misconfigured or the AI is rate
limited, the user gets a clear, in-character message instead of a stack trace.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from db import config_cache
from services.groq_service import GroqError, GroqRateLimited, GroqService, GroqUnavailable
from utils.logging_setup import get_logger

log = get_logger("cogs.ai")

MAX_PROMPT_LENGTH = 1500

# Shown when the AI is unavailable. Deliberately short and never a stack trace.
FALLBACK_MESSAGE = "My brain is offline right now - try again in a moment."


class AiCog(commands.Cog, name="AI"):
    """Groq-powered chat, gated per guild and per channel."""

    def __init__(
        self, bot: commands.Bot, groq_service: GroqService | None
    ) -> None:
        self.bot = bot
        self.groq = groq_service

    # --- slash -------------------------------------------------------------

    @app_commands.command(name="ask", description="Ask Candy a question.")
    @app_commands.describe(prompt="What you want to ask.")
    async def ask(self, interaction: discord.Interaction, prompt: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "AI chat is only available inside a server.", ephemeral=True
            )
            return

        config = config_cache.get(interaction.guild_id)

        if not config.ai_enabled:
            await interaction.response.send_message(
                "AI chat is disabled here. An admin can enable it with `/config chat toggle`.",
                ephemeral=True,
            )
            return

        if len(prompt) > MAX_PROMPT_LENGTH:
            await interaction.response.send_message(
                f"That prompt is too long (max {MAX_PROMPT_LENGTH} characters).",
                ephemeral=True,
            )
            return

        # /ask works regardless of the channel allow-list, but honours cooldown.
        await interaction.response.defer(thinking=True)
        try:
            reply = await self.groq.respond_to_command(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id or 0,
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                prompt=prompt,
                context_messages=self._context_count(config),
                system_prompt=config.ai_system_prompt,
                model=config.ai_model,
                max_tokens=config.ai_max_tokens,
                temperature=config.ai_temperature,
                cooldown_seconds=config.ai_user_cooldown_seconds,
            )
        except GroqRateLimited as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except (GroqUnavailable, GroqError) as exc:
            log.error("AI request failed: %s", exc, exc_info=True)
            await interaction.followup.send(FALLBACK_MESSAGE, ephemeral=True)
            return

        await interaction.followup.send(self._trim(reply))

    # --- mention handling --------------------------------------------------

    async def handle_mention(
        self, message: discord.Message, cleaned_prompt: str
    ) -> bool:
        """Respond to a mention. Returns True when a reply was attempted."""
        if message.guild is None or self.groq is None:
            return False

        config = config_cache.get(message.guild.id)
        channel_id = message.channel.id if message.channel else 0

        # Channel gating: empty allow-list means DISABLED, not "everywhere".
        if not config.ai_allowed_in(channel_id):
            return False

        await message.channel.typing()

        try:
            reply = await self.groq.respond_to_mention(
                guild_id=message.guild.id,
                channel_id=channel_id,
                user_id=message.author.id,
                user_name=message.author.display_name,
                prompt=cleaned_prompt,
                context_messages=self._context_count(config),
                system_prompt=config.ai_system_prompt,
                model=config.ai_model,
                max_tokens=config.ai_max_tokens,
                temperature=config.ai_temperature,
                cooldown_seconds=config.ai_user_cooldown_seconds,
            )
        except GroqRateLimited as exc:
            await message.reply(str(exc), mention_author=False)
            return True
        except (GroqUnavailable, GroqError) as exc:
            log.error("AI mention handling failed: %s", exc, exc_info=True)
            await message.reply(FALLBACK_MESSAGE, mention_author=False)
            return True

        await message.reply(self._trim(reply), mention_author=False)
        return True

    async def reset_context(self, channel_id: int) -> None:
        if self.groq is not None:
            self.groq.reset_channel(channel_id)

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _context_count(config) -> int:
        # History is kept per channel; 0 means "no history".
        return max(0, config.ai_context_messages)

    @staticmethod
    def _trim(text: str, limit: int = 1900) -> str:
        """Keep replies inside Discord's 2000-character message limit."""
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AiCog(bot, getattr(bot, "groq_service", None)))
