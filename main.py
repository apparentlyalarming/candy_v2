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
import os
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

# A prefix discord.py's built-in command processor can never match. The bot
# dispatches prefix commands itself (see CandyBot._prefix_resolver), so the
# built-in machinery must stay out of the way. A NUL byte cannot appear in a
# Discord message, and message.content never starts with one.
PREFIX_SENTINEL = "\x00"

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

    def __init__(self, repair_mode: bool = False) -> None:
        """repair_mode builds the command tree and nothing else.

        Used by the --clear-commands / CLEAR_COMMANDS one-shot repair. In that
        mode setup_hook must NOT start the realtime listener, the rotation
        reminder, or the normal sync: the process only needs an HTTP session to
        rewrite Discord's registrations, then exits. Starting real services here
        would leak background tasks and, worse, run a NORMAL sync before the
        clear - which is the very thing the repair is undoing.
        """
        self.repair_mode = repair_mode

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

    async def _prefix_resolver(self, bot: commands.Bot, message: discord.Message) -> list[str]:
        """Return a prefix the built-in processor can never match.

        Prefix commands are dispatched by GeneralCog.on_message, NOT by discord.py's
        built-in prefix machinery - this bot registers no @commands.command at all.
        So the resolver must NOT return the guild's real prefix: doing that makes the
        built-in processor treat every "!hug" / "!candy" / "!help" as an attempted
        command, find nothing registered, and log a CommandNotFound error for each
        one. Returning the sentinel short-circuits that, which is what the old
        comment claimed while the code quietly did the opposite.
        """
        return [PREFIX_SENTINEL]

    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        """Runs after login, before the gateway connects."""
        log.info("Starting candy_v2 (Python %s, discord.py %s)", sys.version.split()[0], discord.__version__)

        if self.repair_mode:
            # Load cogs only, so the command tree is populated, then clear and
            # rebuild. No Supabase, no realtime, no reminders - this process
            # exists solely to rewrite Discord's registrations.
            log.warning("Repair mode: skipping services, loading cogs only")
            await self._load_cogs()
            await self._clear_and_resync()
            return

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
                "Check SUPABASE_URL / SUPABASE_SERVICE_KEY and that supabase/schema.sql has been run.",
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

    async def _clear_and_resync(self) -> None:
        """One-shot repair: wipe every registration, then re-sync.

        WHY THIS EXISTS
            App command registrations live on Discord's side and OUTLIVE the
            bot's code. If an earlier deploy ran with DEV_GUILD_IDS empty, it
            registered commands GLOBALLY. Setting DEV_GUILD_IDS later makes the
            bot register into the GUILD scope instead - but it never removes the
            old global copies, because nothing in the normal sync path clears.

            The result is two live registrations of the same command name. Since
            CommandTree.fallback_to_global defaults to True, an invocation in the
            guild can be served by the STALE GLOBAL one, whose parameter list no
            longer matches the code. Discord then fails the interaction with:

                CommandSignatureMismatch: The signature for command 'x' is
                different from the one provided by Discord.

            A guild sync alone cannot fix this. The stale entry is global, so a
            global clear is the only thing that removes it.

        WHAT IT DOES
            1. snapshots the currently-registered commands (they are the SOURCE
               for the guild copies, so they must survive the wipe)
            2. clears the global scope and syncs, so Discord drops the stale globals
            3. clears each dev guild's scope and syncs, dropping stale guild copies
            4. restores the global commands and copies them into each dev guild

        THE TRAP THIS AVOIDS
            clear_commands(guild=None) empties the GLOBAL scope, and
            copy_global_to(guild=...) copies FROM that scope. Clearing first and
            copying afterwards therefore re-registers NOTHING - it would wipe the
            whole command tree instead of repairing it. So the commands are
            snapshotted up front and put back before any copy.
        """
        log.warning("Clear-and-resync requested: wiping ALL command registrations")

        # 1) Snapshot. These come from the cogs that setup_hook already loaded,
        #    and they are the source for every guild copy below.
        snapshot = list(self.tree.get_commands(guild=None))
        if not snapshot:
            log.error(
                "Refusing to clear: the tree is empty, so there is nothing to "
                "restore. This would delete every command. Are the cogs loaded?"
            )
            return
        log.info("Snapshot holds %d commands to restore", len(snapshot))

        # 2) Global scope. An empty sync is how discord.py deletes commands.
        self.tree.clear_commands(guild=None)
        n = await self.tree.sync()
        log.info("Cleared global scope; Discord now holds %d global commands", len(n))

        # 3) Every dev guild scope.
        for guild_id in settings.dev_guild_ids:
            guild = discord.Object(id=guild_id)
            self.tree.clear_commands(guild=guild)
            n = await self.tree.sync(guild=guild)
            log.info("Cleared guild scope %s; Discord now holds %d commands", guild_id, len(n))

        # 4) Put the commands back, then re-register. Skip step 1's branch in
        #    _sync_commands by doing the copy explicitly for the dev guilds.
        for cmd in snapshot:
            self.tree.add_command(cmd)

        if settings.dev_guild_ids:
            for guild_id in settings.dev_guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                n = await self.tree.sync(guild=guild)
                log.info("Re-registered %d commands to guild %s", len(n), guild_id)
        else:
            n = await self.tree.sync()
            log.info("Re-registered %d commands globally", len(n))

        log.info("Clear-and-resync complete")

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

    # One-shot command-tree repair. Triggered either by a CLI flag (if the host
    # exposes a start command) or by an env var (if it only exposes variables).
    # See CandyBot._clear_and_resync for why a stale registration needs this.
    repair = (
        "--clear-commands" in sys.argv
        or os.getenv("CLEAR_COMMANDS", "").strip().lower() in ("1", "true", "yes")
    )

    bot = CandyBot(repair_mode=repair)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_shutdown(signal_name: str) -> None:
        log.info("Received %s; shutting down", signal_name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown, sig.name)

    if repair:
        # The command tree needs only a REST session and the application id, so
        # login() is enough - its setup_hook call does the clear and re-sync.
        log.warning("CLEAR_COMMANDS active - wiping and rebuilding the command tree, then exiting")
        await bot.login(settings.discord_token)
        await bot.close()
        log.warning("Repair mode finished. REMOVE CLEAR_COMMANDS / --clear-commands now.")
        return

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
