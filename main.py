"""candy_v2 — entry point.

Startup order matters and is deliberate:

  1. logging
  2. Supabase connectivity check
  3. bulk-load the settings cache       <- handlers read memory, never the network
  4. attach the realtime listener       <- keeps the cache fresh from here on
  5. load cogs and sync the command tree
  6. connect to Discord

Loading the cache before attaching the listener avoids a window where a change
could be missed. Attaching before the gateway connects means config is ready by
the time the first command arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

import discord
from discord.ext import commands

from config import settings
from db import realtime
from services.config_service import ConfigService
from services.groq_service import GroqService
from services.rotation_reminder import RenewalButtonView, RotationReminder, build_reminder
from utils.event_log import make_emitter
from utils.logging_setup import get_logger, setup_logging

log = get_logger("main")

# Cogs loaded at startup. ORDER MATTERS: config_cog must load first because
# cogs/qotd_cog.py parents its /config qotd subtree to config_group. Loading
# qotd_cog first would raise CommandAlreadyRegistered on the root group.
COGS = [
    "cogs.config_cog",
    "cogs.general_cog",
    "cogs.fun_cog",
    "cogs.qotd_cog",
    "cogs.ai_cog",
]


class CandyBot(commands.Bot):
    """discord.py client with the shared services attached."""

    def __init__(self) -> None:
        # Intents: message_content enables prefix commands (!hug). members is
        # enabled because prefix-command admin checks read guild_permissions.
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True

        super().__init__(
            command_prefix=self._prefix_resolver,
            intents=intents,
            help_command=None,
            # Members are cached for permission checks on prefix commands.
            member_cache_flags=discord.MemberCacheFlags.from_intents(intents),
        )

        self.config_service: ConfigService = ConfigService()
        self.groq_service: GroqService | None = None
        self.realtime_listener: realtime.RealtimeListener | None = None
        self.rotation_reminder: RotationReminder | None = None

        # Prefix commands are dispatched by the general cog, not by the built-in
        # prefix machinery, so we return a prefix the bot would never match.
        self._prefix_cache: str = "\x00"

    async def _prefix_resolver(self, bot: commands.Bot, message: discord.Message) -> list[str]:
        """Per-guild prefix lookup from the cache."""
        if message.guild is None:
            return []
        try:
            prefix = self.config_service.get(message.guild.id).prefix
            return [prefix]
        except Exception:  # noqa: BLE001 - never break dispatch on a config hiccup
            return [settings.command_prefix]

    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        """Runs after login, before the gateway connects."""
        log.info("Starting candy_v2 (Python %s, discord.py %s)", sys.version.split()[0], discord.__version__)

        # 1. Verify Supabase is reachable and load the cache.
        await self._load_config_cache()

        # 2. Realtime listener keeps the cache fresh.
        if settings.enable_realtime:
            self.realtime_listener = realtime.RealtimeListener(
                on_settings_changed=self._on_settings_changed
            )
            self.realtime_listener.start()
        else:
            log.warning("Realtime is disabled by config; config changes will not apply live")

        # 3. Groq service (talks to the chat Edge Function, holds no provider key).
        self.groq_service = GroqService(
            supabase_url=settings.supabase_url,
            service_key=settings.supabase_service_key,
        )
        if not settings.has_groq:
            log.info("GROQ_API_KEY not set on the bot - that is expected; the chat Edge Function holds it.")

        # 4. Event logging (log channel + dashboard feed).
        emitter = make_emitter(self)
        self.emit_log = emitter  # type: ignore[attr-defined]

        # 5. Host rotation reminder. The host is bot-hosting.net, whose free tier
        #    stops the bot unless renewed every 4 days; the bot cannot read that
        #    timer, so this watches its own uptime. Started here rather than in
        #    on_ready so the clock exists even if Discord connects slowly.
        if settings.rotation_reminder_days > 0:
            self.rotation_reminder = build_reminder(
                bot=self,
                log_file=settings.log_file,
                threshold_days=settings.rotation_reminder_days,
            )
            self.rotation_reminder.start()

            # Persistent view: reminder DMs sent by this or any PREVIOUS process
            # keep a working button. Without this registration the button dies on
            # restart, which is precisely when it is most needed - renewing the
            # host restarts the bot, and the owner then clicks the old message.
            self.add_view(RenewalButtonView(self.rotation_reminder))

        # 6. Load cogs.
        await self._load_cogs()

        # 7. Sync the command tree.
        await self._sync_commands()

        log.info("Setup complete; connecting to Discord")

    async def _load_config_cache(self) -> None:
        try:
            count = await asyncio.to_thread(realtime.load_initial_cache)
            log.info("Config cache primed with %d guild(s)", count)
        except Exception as exc:
            log.error(
                "Could not load the config cache: %s. "
                "Check SUPABASE_URL / SUPABASE_SERVICE_KEY and that schema.sql has been run.",
                exc,
                exc_info=True,
            )
            log.warning("Continuing with default settings for all guilds")

    async def _load_cogs(self) -> None:
        for extension in COGS:
            try:
                await self.load_extension(extension)
                log.info("Loaded %s", extension)
            except Exception as exc:
                log.error("Failed to load %s: %s", extension, exc, exc_info=True)

    async def _sync_commands(self) -> None:
        """Sync the app command tree.

        Cloning to specific dev guilds when DEV_GUILD_IDS is set gives instant
        command updates; a global sync can take up to an hour to propagate.
        """
        try:
            if settings.dev_guild_ids:
                for guild_id in settings.dev_guild_ids:
                    guild = discord.Object(id=guild_id)
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                    log.info("Synced %d commands to dev guild %s", len(synced), guild_id)
            else:
                synced = await self.tree.sync()
                log.info("Synced %d commands globally", len(synced))
        except Exception as exc:
            log.error("Command sync failed: %s", exc, exc_info=True)

    # ------------------------------------------------------------------

    async def _on_settings_changed(self, guild_id: int, row: dict) -> None:
        """Called by the realtime listener after the cache is updated."""
        log.debug("Settings changed for guild %s", guild_id)

    async def on_ready(self) -> None:
        log.info(
            "Ready as %s (id %s) in %d guild(s)",
            self.user,
            self.user.id if self.user else "?",
            len(self.guilds),
        )

        # Refresh guild metadata and make sure every guild has a settings row.
        await self._refresh_guilds()

    async def _refresh_guilds(self) -> None:
        for guild in self.guilds:
            try:
                await asyncio.to_thread(
                    self.config_service.ensure_guild_row,
                    guild.id,
                    name=guild.name,
                    icon_url=str(guild.icon.url) if guild.icon else None,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not refresh guild %s: %s", guild.id, exc)

    async def close(self) -> None:
        """Graceful shutdown: stop background tasks, close sockets."""
        log.info("Shutting down...")

        if self.realtime_listener is not None:
            with contextlib.suppress(Exception):
                await self.realtime_listener.stop()

        if self.rotation_reminder is not None:
            with contextlib.suppress(Exception):
                await self.rotation_reminder.stop()

        if self.groq_service is not None:
            with contextlib.suppress(Exception):
                await self.groq_service.aclose()

        with contextlib.suppress(Exception):
            await super().close()

        log.info("Shutdown complete")


async def main() -> None:
    setup_logging(settings.log_level, settings.log_file)

    bot = CandyBot()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_shutdown(signal_name: str) -> None:
        log.info("Received %s; shutting down", signal_name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown, sig.name)

    async with bot:
        try:
            await bot.start(settings.discord_token)
        except asyncio.CancelledError:
            pass
        finally:
            if not bot.is_closed():
                await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        logging_logger = get_logger("main")
        logging_logger.critical("Fatal error: %s", exc, exc_info=True)
        raise SystemExit(1)
