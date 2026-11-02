"""Incoming DM forwarding and admin bridge listener.

The background work lives in ``services/admin_bridge.py``; this cog only
translates Discord events into calls on it. DMs are the sole event it cares
about - everything else the bridge does is time-driven.

Kept as a separate cog (rather than folding into general_cog) because forwarding
and database capture are distinct concerns. Forwarding uses an environment
configured Discord ID; database capture can be switched off with CAPTURE_DMS.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from utils.logging_setup import get_logger

log = get_logger("cogs.admin")


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # One-to-one DMs only. Group DMs and guild messages are out of scope:
        # guild chatter belongs to ai_memory, not the operator inbox.
        if not isinstance(message.channel, discord.DMChannel):
            return
        if message.author.bot:
            return

        # Forward directly through Discord using an environment-configured
        # recipient. This path is independent of Supabase and remains available
        # even when database capture or the admin bridge is disabled.
        recipient_id = getattr(self.bot, "dm_forward_user_id", None)
        if recipient_id:
            await self._forward_dm(message, recipient_id)

        if not getattr(self.bot, "capture_dms", False):
            return

        bridge = getattr(self.bot, "admin_bridge", None)
        if bridge is not None:
            await bridge.record_dm(message)

    async def _forward_dm(self, message: discord.Message, recipient_id: int) -> None:
        """Forward an incoming DM as an embed without touching the database."""
        try:
            recipient = self.bot.get_user(recipient_id) or await self.bot.fetch_user(recipient_id)
            sender_name = getattr(message.author, "display_name", None) or message.author.name
            sender_label = f"{sender_name} (@{message.author.name})"
            content = (message.content or "").strip() or "(No text content)"

            embed = discord.Embed(
                title="Incoming bot DM",
                description=content,
                color=discord.Color.blurple(),
                timestamp=message.created_at,
            )
            embed.add_field(name="Sender", value=sender_label[:256], inline=False)
            embed.add_field(name="Discord user ID", value=str(message.author.id), inline=False)
            if message.attachments:
                attachment_lines = [f"[{a.filename}]({a.url})" for a in message.attachments[:10]]
                embed.add_field(name="Attachments", value="\n".join(attachment_lines)[:1024], inline=False)

            await recipient.send(embed=embed)
        except discord.Forbidden:
            log.warning("Could not forward DM: configured recipient %s does not accept bot DMs", recipient_id)
        except discord.NotFound:
            log.warning("Could not forward DM: configured recipient %s was not found", recipient_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not forward DM to configured recipient %s: %s", recipient_id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
