"""Admin bridge event listener.

The background work lives in ``services/admin_bridge.py``; this cog only
translates Discord events into calls on it. DMs are the sole event it cares
about - everything else the bridge does is time-driven.

Kept as a separate cog (rather than folding into general_cog) because capture is
a distinct, privacy-sensitive concern: it can be switched off with CAPTURE_DMS
without touching command handling, and its failures must not affect dispatch.
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
        if not getattr(self.bot, "capture_dms", False):
            return

        bridge = getattr(self.bot, "admin_bridge", None)
        if bridge is None:
            return
        await bridge.record_dm(message)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
