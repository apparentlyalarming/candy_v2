"""Dropdown menus for the bot's large command groups.

WHY THIS EXISTS
    /config, /config qotd and /report together expose 45+ slash commands. The
    command picker is the only discovery surface Discord gives us, and names
    like ``/config chat memorychannels`` are hard to remember. This module adds
    ``/config menu``, ``/qotd menu`` and ``/report menu`` as *alternate* entry
    points: the slash commands stay exactly where they are, and the menu reads
    their existing :class:`app_commands.Command` objects rather than
    reimplementing any settings logic.

DESIGN RULES
    - Menus are additive. Nothing is moved or removed.
    - A command with no inputs runs immediately.
    - Predefined input (bool, Choice, channel, role, user) becomes a select.
    - Free-form input (text, numbers) becomes a modal.
    - Mixed input collects the selects first, then opens the modal.
    - Optional channel/role/user parameters get a Clear button.
    - Menus are ephemeral, guild-only, locked to the invoker, and require
      Manage Server. The Dev category is hidden from non-owners.
    - A menu never lists itself.

HOW A COMMAND IS INVOKED
    ``bind_group_commands`` (cogs/__init__.py) walks the tree after each cog is
    added and sets ``command.binding`` to the owning cog instance. That means
    the menu can call the *same* callback Discord uses::

        await command.callback(command.binding, proxy, **kwargs)

    The proxy captures ``response.send_message`` output (the interaction is
    already acknowledged by the menu) and forwards everything else, including
    ``followup.send`` so file attachments keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import discord
from discord import app_commands

from utils.logging_setup import get_logger

log = get_logger("cogs.menu_ui")

MENU_TIMEOUT = 300.0
SKIP = "__skip__"

WEEKDAYS: tuple[tuple[str, int], ...] = (
    ("Monday", 1),
    ("Tuesday", 2),
    ("Wednesday", 3),
    ("Thursday", 4),
    ("Friday", 5),
    ("Saturday", 6),
    ("Sunday", 7),
)

BRAND_COLOUR = discord.Colour.from_str("#FFB6C1")

# Commands whose parameters need a hand-written control instead of the generic
# one-field-per-select wizard. Keyed by ``Command.qualified_name``.
SPECIAL_COMMANDS = {
    "config qotd days": "weekdays",
    "config report staffroles": "staffroles",
}


# ---------------------------------------------------------------------------
# permission helpers
# ---------------------------------------------------------------------------


def can_manage(member: discord.abc.User | discord.Member | None) -> bool:
    """Mirror the Manage Server gate Discord applies to /config."""
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    return bool(perms.manage_guild or perms.administrator)


def _truncate(text: str | None, limit: int) -> str:
    value = (text or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# menu description model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One row in a menu page: either a command or a link to another page."""

    label: str
    description: str
    command: app_commands.Command | None = None
    owner: Any = None
    page: str | None = None
    emoji: str | None = None


@dataclass(frozen=True)
class Page:
    key: str
    title: str
    description: str
    items: tuple[Item, ...]


@dataclass(frozen=True)
class MenuSpec:
    key: str
    title: str
    description: str
    root: str
    pages: dict[str, Page]
    parents: dict[str, str] = field(default_factory=dict)

    def page(self, key: str) -> Page:
        return self.pages[key]

    def parent_of(self, key: str) -> str | None:
        return self.parents.get(key)


def command_items(
    commands: Iterable[Any],
    owner: Any,
    *,
    exclude: Sequence[str] = ("menu",),
) -> tuple[Item, ...]:
    """Turn leaf commands into menu items, preserving their descriptions."""
    items: list[Item] = []
    for command in commands:
        if not isinstance(command, app_commands.Command):
            continue
        if command.name in exclude:
            continue
        items.append(
            Item(
                label=command.name,
                description=_truncate(command.description, 100)
                or "No description.",
                command=command,
                owner=owner,
            )
        )
    return tuple(items)


def page_item(
    label: str, description: str, page: str, emoji: str | None = None
) -> Item:
    return Item(label=label, description=description, page=page, emoji=emoji)


# ---------------------------------------------------------------------------
# parameter classification
# ---------------------------------------------------------------------------


def _params(command: app_commands.Command) -> list[Any]:
    """Return the command's public Parameter objects.

    discord.py exposes ``Command.parameters``; ``_params`` is a fallback for
    older 2.x releases. Both carry the fields the menu needs (name, type,
    choices, required, min/max).
    """
    parameters = getattr(command, "parameters", None)
    if parameters is not None:
        try:
            return list(parameters)
        except TypeError:  # property in an unexpected shape
            pass
    return list(getattr(command, "_params", {}).values())


def _kind(param: Any) -> str:
    """Classify a command parameter as a select or a modal field."""
    option_type = getattr(param, "type", None)
    choices = getattr(param, "choices", None)
    if option_type is discord.AppCommandOptionType.boolean:
        return "bool"
    if choices:
        return "choice"
    if option_type in (
        discord.AppCommandOptionType.channel,
        discord.AppCommandOptionType.role,
        discord.AppCommandOptionType.user,
        discord.AppCommandOptionType.mentionable,
    ):
        return "entity"
    if option_type in (
        discord.AppCommandOptionType.integer,
        discord.AppCommandOptionType.number,
    ):
        return "number"
    return "text"


# ---------------------------------------------------------------------------
# embed + view rendering
# ---------------------------------------------------------------------------


def _page_embed(spec: MenuSpec, page: Page, *, note: str | None = None) -> discord.Embed:
    lines: list[str] = []
    for item in page.items:
        if item.page is not None:
            lines.append(f"**{item.label}** — {item.description}")
        elif item.command is not None:
            lines.append(
                f"`/{item.command.qualified_name}` — {item.description}"
            )
    body = "\n".join(lines) or "Nothing is available here yet."
    if len(body) > 3900:
        body = body[:3899] + "…"
    if note:
        body = f"{note}\n\n{body}"
    title = (
        spec.title
        if page.key == spec.root
        else f"{spec.title} · {page.title}"
    )
    embed = discord.Embed(
        title=title,
        description=body,
        colour=BRAND_COLOUR,
    )
    embed.set_footer(
        text="You can still use the slash commands directly. Menus close after 5 minutes."
    )
    return embed


def _chunks(items: Sequence[Any], size: int) -> list[Sequence[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class _PageSelect(discord.ui.Select):
    def __init__(self, session: "MenuSession", items: Sequence[Item]) -> None:
        self.session = session
        options = [
            discord.SelectOption(
                label=_truncate(item.label, 100),
                value=item.page or "",
                description=_truncate(item.description, 100) or None,
                emoji=item.emoji,
            )
            for item in items
        ]
        super().__init__(placeholder="Choose a category…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.session.show(interaction, self.values[0])


class _CommandSelect(discord.ui.Select):
    def __init__(
        self, session: "MenuSession", items: Sequence[Item], placeholder: str
    ) -> None:
        self.session = session
        self.items = list(items)
        options = [
            discord.SelectOption(
                label=_truncate(item.command.name if item.command else item.label, 100),
                value=str(index),
                description=_truncate(item.description, 100) or None,
                emoji=item.emoji,
            )
            for index, item in enumerate(self.items)
        ]
        super().__init__(placeholder=placeholder, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        item = self.items[int(self.values[0])]
        await self.session.start_command(interaction, item)


class _MenuView(discord.ui.View):
    def __init__(self, session: "MenuSession", page: Page) -> None:
        super().__init__(timeout=MENU_TIMEOUT)
        self.session = session
        self.page = page

        page_links = [item for item in page.items if item.page is not None]
        commands = [item for item in page.items if item.command is not None]

        parent = session.spec.parent_of(page.key)
        # Discord allows five action rows per message. Reserve rows for the
        # Back and Close buttons before adding selects, so a page that grows
        # past 50 commands cannot make the whole view invalid.
        rows_left = 5 - 1 - (1 if parent is not None else 0)
        for chunk in _chunks(page_links, 25):
            if rows_left <= 0:
                log.warning("Menu page %s has more categories than fit", page.key)
                break
            if chunk:
                self.add_item(_PageSelect(session, chunk))
                rows_left -= 1
        command_index = 0
        for chunk in _chunks(commands, 25):
            if rows_left <= 0:
                log.warning("Menu page %s has more commands than fit", page.key)
                break
            if chunk:
                placeholder = (
                    "Choose a command…"
                    if command_index == 0
                    else f"More commands ({command_index + 1})…"
                )
                self.add_item(_CommandSelect(session, chunk, placeholder))
                rows_left -= 1
                command_index += 1

        if parent is not None:
            back = discord.ui.Button(
                label="Back",
                style=discord.ButtonStyle.secondary,
                emoji="◀️",
            )
            back.callback = self._back  # type: ignore[method-assign]
            self.add_item(back)

        close = discord.ui.Button(
            label="Close",
            style=discord.ButtonStyle.danger,
            emoji="✖️",
        )
        close.callback = self._close  # type: ignore[method-assign]
        self.add_item(close)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.invoker_id:
            await interaction.response.send_message(
                "This menu belongs to someone else. Run the menu command to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def _back(self, interaction: discord.Interaction) -> None:
        parent = self.session.spec.parent_of(self.page.key) or self.session.spec.root
        await self.session.show(interaction, parent)

    async def _close(self, interaction: discord.Interaction) -> None:
        self.stop()
        if self.session.view is self:
            self.session.view = None
        await interaction.response.edit_message(
            content="Menu closed. The slash commands are still available.",
            embed=None,
            view=None,
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            if self.session.message is not None:
                await self.session.message.edit(view=self)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# interaction proxy
# ---------------------------------------------------------------------------


class _CapturedResponse:
    def __init__(self, proxy: "MenuInteractionProxy") -> None:
        self.proxy = proxy

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        self.proxy.captured.append((args, kwargs))

    async def defer(self, *args: Any, **kwargs: Any) -> None:
        # The menu already acknowledged the component interaction.
        return None

    async def edit_message(self, *args: Any, **kwargs: Any) -> None:
        # No current callback edits its own response; keep it honest if one does.
        self.proxy.captured.append((args, kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.proxy.interaction.response, name)


class MenuInteractionProxy:
    """Thin wrapper that lets an existing command callback run from a menu.

    The component interaction is deferred before the callback starts, so
    ``response.send_message`` cannot be forwarded. Those calls are captured and
    replayed as followups afterwards. Everything else (``user``, ``guild``,
    ``client``, ``followup.send`` with attachments) delegates to the real
    interaction.
    """

    def __init__(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self.captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.response = _CapturedResponse(self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.interaction, name)


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    item: Item
    command: app_commands.Command
    owner: Any
    values: dict[str, Any] = field(default_factory=dict)
    steps: list[Any] = field(default_factory=list)
    index: int = 0
    freeform: list[Any] = field(default_factory=list)

    def kwargs(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for param in _params(self.command):
            if param.name in self.values:
                value = self.values[param.name]
                if value is SKIP:
                    continue
                result[param.name] = value
            # Missing optional values are omitted so the callback's default
            # applies, which is exactly what the slash command would do.
        return result


class MenuSession:
    def __init__(self, interaction: discord.Interaction, spec: MenuSpec) -> None:
        self.spec = spec
        self.invoker_id = interaction.user.id
        self.guild_id = interaction.guild_id
        self.message: discord.Message | None = None
        self.view: discord.ui.View | None = None
        self.pending: _Pending | None = None

    # -- rendering -----------------------------------------------------

    def _view(self, page: Page) -> _MenuView:
        return _MenuView(self, page)

    def _swap_view(self, view: discord.ui.View | None) -> discord.ui.View | None:
        """Stop the previous view so its timeout cannot edit the message.

        Every navigation replaces the view on the same message. discord.py keeps
        the old View's timeout task alive unless it is stopped, so without this
        an earlier page could re-appear a few minutes later with disabled
        controls.
        """
        previous = self.view
        self.view = view
        if previous is not None and previous is not view:
            previous.stop()
        return view

    async def open(self, interaction: discord.Interaction) -> None:
        page = self.spec.page(self.spec.root)
        view = self._swap_view(self._view(page))
        await interaction.response.send_message(
            embed=_page_embed(self.spec, page),
            view=view,
            ephemeral=True,
        )
        self.message = await interaction.original_response()

    async def _edit(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        view: discord.ui.View,
        content: str | None = None,
    ) -> None:
        """Update the menu message from any kind of interaction.

        A modal submit cannot use ``response.edit_message``, and an interaction
        that was deferred must go through the interaction webhook. This helper
        picks the valid path instead of making every caller care.
        """
        view = self._swap_view(view)  # type: ignore[assignment]
        if not interaction.response.is_done():
            await interaction.response.edit_message(
                content=content, embed=embed, view=view
            )
            return
        await interaction.edit_original_response(
            content=content, embed=embed, view=view
        )

    async def show(self, interaction: discord.Interaction, key: str) -> None:
        if key not in self.spec.pages:
            key = self.spec.root
        page = self.spec.page(key)
        self.pending = None
        await self._edit(
            interaction,
            embed=_page_embed(self.spec, page),
            view=self._view(page),
        )

    async def _show_note(
        self,
        interaction: discord.Interaction,
        key: str,
        note: str,
    ) -> None:
        page = self.spec.page(key)
        await self._edit(
            interaction,
            embed=_page_embed(self.spec, page, note=note),
            view=self._view(page),
        )

    # -- command flow --------------------------------------------------

    async def start_command(self, interaction: discord.Interaction, item: Item) -> None:
        command = item.command
        if command is None:
            await self.show(interaction, self.spec.root)
            return

        owner = item.owner if item.owner is not None else getattr(command, "binding", None)
        if owner is None:
            log.error("Menu command %s has no bound cog", command.qualified_name)
            await interaction.response.send_message(
                "That command is not ready yet. Try the slash command directly.",
                ephemeral=True,
            )
            return

        pending = _Pending(item=item, command=command, owner=owner)
        self.pending = pending

        special = SPECIAL_COMMANDS.get(command.qualified_name)
        if special is not None:
            await self._show_special(interaction, pending, special)
            return

        params = _params(command)
        for param in params:
            kind = _kind(param)
            if kind in {"bool", "choice", "entity"}:
                pending.steps.append(param)
            else:
                pending.freeform.append(param)

        if not pending.steps and not pending.freeform:
            await self._execute(interaction, pending)
            return
        if not pending.steps:
            await interaction.response.send_modal(_ParamModal(self, pending))
            return
        await self._next_step(interaction, pending)

    async def _next_step(
        self, interaction: discord.Interaction, pending: _Pending
    ) -> None:
        if pending.index >= len(pending.steps):
            if pending.freeform:
                await interaction.response.send_modal(_ParamModal(self, pending))
            else:
                await self._execute(interaction, pending)
            return

        param = pending.steps[pending.index]
        view = _ParamView(self, pending, param)
        embed = _param_embed(pending, pending.index + 1, len(pending.steps))
        await self._edit(interaction, embed=embed, view=view)

    async def advance(
        self,
        interaction: discord.Interaction,
        pending: _Pending,
        value: Any,
    ) -> None:
        param = pending.steps[pending.index]
        pending.values[param.name] = value
        pending.index += 1
        await self._next_step(interaction, pending)

    async def _show_special(
        self,
        interaction: discord.Interaction,
        pending: _Pending,
        special: str,
    ) -> None:
        if special == "weekdays":
            view: discord.ui.View = _WeekdayView(self, pending)
        elif special == "staffroles":
            view = _StaffRoleView(self, pending)
        else:
            await self._execute(interaction, pending)
            return

        embed = discord.Embed(
            title=f"/{pending.command.qualified_name}",
            description=pending.command.description or "",
            colour=BRAND_COLOUR,
        )
        embed.set_footer(text="Pick one or more, then the command runs.")
        await self._edit(interaction, embed=embed, view=view)

    # -- execution -----------------------------------------------------

    async def _execute(
        self, interaction: discord.Interaction, pending: _Pending
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()

        proxy = MenuInteractionProxy(interaction)
        error: str | None = None
        try:
            await pending.command.callback(
                pending.owner, proxy, **pending.kwargs()
            )
        except Exception as exc:  # noqa: BLE001 - report, never crash the menu
            log.error(
                "Menu invocation of /%s failed: %s",
                pending.command.qualified_name,
                exc,
                exc_info=True,
            )
            error = "Something went wrong running that command. Check the bot logs."

        # Replay captured response.send_message output as ephermal followups.
        for args, kwargs in proxy.captured:
            payload = dict(kwargs)
            payload.setdefault("ephemeral", True)
            try:
                await interaction.followup.send(*args, **payload)
            except discord.HTTPException as exc:
                log.warning("Could not replay menu output: %s", exc)

        page = self._page_for(pending.item)
        note = (
            error
            if error is not None
            else f"✅ Ran `/{pending.command.qualified_name}`."
        )
        self.pending = None
        await self._show_note(interaction, page, note)

    def _page_for(self, item: Item) -> str:
        for page in self.spec.pages.values():
            if any(candidate is item for candidate in page.items):
                return page.key
        return self.spec.root


def _param_embed(pending: _Pending, step: int, total: int) -> discord.Embed:
    param = pending.steps[pending.index] if pending.index < len(pending.steps) else None
    description = _truncate(param.description, 300) if param is not None else ""
    embed = discord.Embed(
        title=f"/{pending.command.qualified_name}",
        description=description or "Choose a value to continue.",
        colour=BRAND_COLOUR,
    )
    if param is not None:
        hint = "Required" if getattr(param, "required", False) else "Optional"
        embed.add_field(name="Parameter", value=f"`{param.name}` ({hint})", inline=False)
    embed.set_footer(text=f"Step {step} of {total}. Cancel returns to the command list.")
    return embed


# ---------------------------------------------------------------------------
# parameter controls
# ---------------------------------------------------------------------------


class _ParamView(discord.ui.View):
    def __init__(
        self,
        session: MenuSession,
        pending: _Pending,
        param: Any,
    ) -> None:
        super().__init__(timeout=MENU_TIMEOUT)
        self.session = session
        self.pending = pending
        self.param = param
        kind = _kind(param)
        optional = not getattr(param, "required", False)

        if kind == "bool":
            options = [
                discord.SelectOption(label="On", value="true", emoji="✅"),
                discord.SelectOption(label="Off", value="false", emoji="🚫"),
            ]
            if optional:
                options.append(
                    discord.SelectOption(
                        label="Use default", value=SKIP, emoji="↩️"
                    )
                )
            self.add_item(_ValueSelect(session, pending, options, "Choose on or off…"))
        elif kind == "choice":
            choices = list(getattr(param, "choices", []) or [])
            # A select holds at most 25 options. Reserve the last slot for the
            # optional "use default" row instead of overflowing it.
            limit = 24 if optional else 25
            options = [
                discord.SelectOption(
                    label=_truncate(choice.name, 100),
                    value=str(index),
                    description=_truncate(str(choice.value), 100) or None,
                )
                for index, choice in enumerate(choices[:limit])
            ]
            if optional:
                options.append(
                    discord.SelectOption(
                        label="Use default", value=SKIP, emoji="↩️"
                    )
                )
            self.add_item(_ValueSelect(session, pending, options, "Choose an option…"))
        else:
            option_type = getattr(param, "type", None)
            if option_type is discord.AppCommandOptionType.channel:
                self.add_item(_ChannelSelect(session, pending, param))
            elif option_type is discord.AppCommandOptionType.role:
                self.add_item(_RoleSelect(session, pending))
            else:
                self.add_item(_UserSelect(session, pending))
            if optional:
                clear = discord.ui.Button(
                    label="Clear / use default",
                    style=discord.ButtonStyle.secondary,
                    emoji="↩️",
                )
                clear.callback = self._clear  # type: ignore[method-assign]
                self.add_item(clear)

        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            emoji="✖️",
        )
        cancel.callback = self._cancel  # type: ignore[method-assign]
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.invoker_id:
            await interaction.response.send_message(
                "This menu belongs to someone else.", ephemeral=True
            )
            return False
        return True

    async def _clear(self, interaction: discord.Interaction) -> None:
        await self.session.advance(interaction, self.pending, None)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        page = self.session._page_for(self.pending.item)
        self.session.pending = None
        await self.session.show(interaction, page)


class _ValueSelect(discord.ui.Select):
    def __init__(
        self,
        session: MenuSession,
        pending: _Pending,
        options: list[discord.SelectOption],
        placeholder: str,
    ) -> None:
        self.session = session
        self.pending = pending
        super().__init__(placeholder=placeholder, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        param = self.pending.steps[self.pending.index]
        raw = self.values[0]
        if raw == SKIP:
            value: Any = SKIP
        elif getattr(param, "type", None) is discord.AppCommandOptionType.boolean:
            value = raw == "true"
        elif getattr(param, "choices", None):
            value = list(param.choices)[int(raw)]
        else:
            value = raw
        await self.session.advance(interaction, self.pending, value)


class _ChannelSelect(discord.ui.ChannelSelect):
    def __init__(
        self, session: MenuSession, pending: _Pending, param: Any
    ) -> None:
        self.session = session
        self.pending = pending
        channel_types = getattr(param, "channel_types", None) or None
        super().__init__(
            placeholder="Choose a channel…",
            min_values=1,
            max_values=1,
            channel_types=channel_types,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        raw = int(interaction.data["values"][0])  # type: ignore[index]
        value = await _resolve_entity(
            interaction, discord.AppCommandOptionType.channel, raw
        )
        await self.session.advance(interaction, self.pending, value)


class _RoleSelect(discord.ui.RoleSelect):
    def __init__(self, session: MenuSession, pending: _Pending) -> None:
        self.session = session
        self.pending = pending
        super().__init__(placeholder="Choose a role…", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        raw = int(interaction.data["values"][0])  # type: ignore[index]
        value = await _resolve_entity(
            interaction, discord.AppCommandOptionType.role, raw
        )
        await self.session.advance(interaction, self.pending, value)


class _UserSelect(discord.ui.UserSelect):
    def __init__(self, session: MenuSession, pending: _Pending) -> None:
        self.session = session
        self.pending = pending
        super().__init__(placeholder="Choose a user…", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        raw = int(interaction.data["values"][0])  # type: ignore[index]
        value = await _resolve_entity(
            interaction, discord.AppCommandOptionType.user, raw
        )
        await self.session.advance(interaction, self.pending, value)


async def _resolve_entity(
    interaction: discord.Interaction, option_type: Any, raw_id: int
) -> Any:
    guild = interaction.guild
    if option_type is discord.AppCommandOptionType.channel:
        getter = getattr(guild, "get_channel_or_thread", None)
        channel = getter(raw_id) if getter is not None else None
        if channel is None and guild is not None:
            channel = guild.get_channel(raw_id)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(raw_id)
            except discord.HTTPException:
                return None
        return channel
    if option_type is discord.AppCommandOptionType.role:
        return guild.get_role(raw_id) if guild else None
    if guild is not None:
        member = guild.get_member(raw_id)
        if member is not None:
            return member
    user = interaction.client.get_user(raw_id)
    if user is not None:
        return user
    try:
        return await interaction.client.fetch_user(raw_id)
    except discord.HTTPException:
        return None


# ---------------------------------------------------------------------------
# special controls
# ---------------------------------------------------------------------------


class _WeekdaySelect(discord.ui.Select):
    def __init__(self, view: "_WeekdayView") -> None:
        self.view_ref = view
        super().__init__(
            placeholder="Pick the days QOTD should post…",
            min_values=1,
            max_values=7,
            options=[
                discord.SelectOption(label=name, value=str(value))
                for name, value in WEEKDAYS
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view_ref.run(interaction, [int(v) for v in self.values])


class _WeekdayView(discord.ui.View):
    def __init__(self, session: MenuSession, pending: _Pending) -> None:
        super().__init__(timeout=MENU_TIMEOUT)
        self.session = session
        self.pending = pending
        self.add_item(_WeekdaySelect(self))
        clear = discord.ui.Button(
            label="Every day (clear)",
            style=discord.ButtonStyle.secondary,
            emoji="↩️",
        )
        clear.callback = self._every_day  # type: ignore[method-assign]
        self.add_item(clear)
        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            emoji="✖️",
        )
        cancel.callback = self._cancel  # type: ignore[method-assign]
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.invoker_id:
            await interaction.response.send_message(
                "This menu belongs to someone else.", ephemeral=True
            )
            return False
        return True

    async def _cancel(self, interaction: discord.Interaction) -> None:
        page = self.session._page_for(self.pending.item)
        self.session.pending = None
        await self.session.show(interaction, page)

    async def _every_day(self, interaction: discord.Interaction) -> None:
        await self.run(interaction, [])

    async def run(self, interaction: discord.Interaction, days: list[int]) -> None:
        chosen = {
            value: app_commands.Choice(name=name, value=value)
            for name, value in WEEKDAYS
        }
        values: dict[str, Any] = {}
        for index in range(1, 8):
            values[f"day{index}"] = chosen.get(days[index - 1]) if index <= len(days) else None
        self.pending.values.update(values)
        await self.session._execute(interaction, self.pending)


class _StaffRoleSelect(discord.ui.RoleSelect):
    def __init__(self, view: "_StaffRoleView") -> None:
        self.view_ref = view
        super().__init__(
            placeholder="Pick up to five staff roles…",
            min_values=0,
            max_values=5,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        raw_ids = [int(v) for v in (interaction.data or {}).get("values", [])]  # type: ignore[union-attr]
        guild = interaction.guild
        roles = [guild.get_role(role_id) for role_id in raw_ids] if guild else []
        await self.view_ref.run(interaction, [r for r in roles if r is not None])


class _StaffRoleView(discord.ui.View):
    def __init__(self, session: MenuSession, pending: _Pending) -> None:
        super().__init__(timeout=MENU_TIMEOUT)
        self.session = session
        self.pending = pending
        self.add_item(_StaffRoleSelect(self))
        clear = discord.ui.Button(
            label="Clear staff roles",
            style=discord.ButtonStyle.secondary,
            emoji="↩️",
        )
        clear.callback = self._clear  # type: ignore[method-assign]
        self.add_item(clear)
        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            emoji="✖️",
        )
        cancel.callback = self._cancel  # type: ignore[method-assign]
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.invoker_id:
            await interaction.response.send_message(
                "This menu belongs to someone else.", ephemeral=True
            )
            return False
        return True

    async def _clear(self, interaction: discord.Interaction) -> None:
        await self.run(interaction, [])

    async def _cancel(self, interaction: discord.Interaction) -> None:
        page = self.session._page_for(self.pending.item)
        self.session.pending = None
        await self.session.show(interaction, page)

    async def run(
        self, interaction: discord.Interaction, roles: list[discord.Role]
    ) -> None:
        selected = roles[:5]
        values: dict[str, Any] = {}
        for index in range(1, 6):
            values[f"role{index}"] = selected[index - 1] if index <= len(selected) else None
        self.pending.values.update(values)
        await self.session._execute(interaction, self.pending)


# ---------------------------------------------------------------------------
# modal
# ---------------------------------------------------------------------------


class _ParamModal(discord.ui.Modal):
    def __init__(self, session: MenuSession, pending: _Pending) -> None:
        title = _truncate(f"/{pending.command.qualified_name}", 45)
        super().__init__(title=title)
        self.session = session
        self.pending = pending
        self.fields: dict[str, discord.ui.TextInput] = {}

        # Discord caps a modal at five text inputs. Every current command has
        # three or fewer free-form parameters; this guard keeps a future one
        # from silently dropping fields.
        for param in pending.freeform[:5]:
            placeholder = ""
            minimum = getattr(param, "min_value", None)
            maximum = getattr(param, "max_value", None)
            if minimum is not None or maximum is not None:
                placeholder = f"{minimum if minimum is not None else '…'} to {maximum if maximum is not None else '…'}"
            if _kind(param) in {"number"}:
                placeholder = placeholder or "A number"
            field = discord.ui.TextInput(
                label=_truncate(param.description or param.name, 45) or param.name,
                placeholder=_truncate(placeholder, 100),
                required=bool(getattr(param, "required", False)),
                max_length=4000 if _kind(param) == "text" else 32,
            )
            self.add_item(field)
            self.fields[param.name] = field

    async def on_submit(self, interaction: discord.Interaction) -> None:
        for param in self.pending.freeform[:5]:
            raw = str(self.fields[param.name].value or "").strip()
            if not raw:
                if getattr(param, "required", False):
                    await self.session._show_note(
                        interaction,
                        self.session._page_for(self.pending.item),
                        f"⚠️ `{param.name}` is required. Run `/{self.pending.command.qualified_name}` again.",
                    )
                    return
                continue
            kind = _kind(param)
            try:
                if kind == "number":
                    option_type = getattr(param, "type", None)
                    value: Any = (
                        int(raw)
                        if option_type is discord.AppCommandOptionType.integer
                        else float(raw)
                    )
                    minimum = getattr(param, "min_value", None)
                    maximum = getattr(param, "max_value", None)
                    if minimum is not None and value < minimum:
                        raise ValueError(f"must be at least {minimum}")
                    if maximum is not None and value > maximum:
                        raise ValueError(f"must be at most {maximum}")
                else:
                    value = raw
            except (TypeError, ValueError) as exc:
                await self.session._show_note(
                    interaction,
                    self.session._page_for(self.pending.item),
                    f"⚠️ `{param.name}`: {exc}. Run `/{self.pending.command.qualified_name}` again.",
                )
                return
            self.pending.values[param.name] = value

        await self.session._execute(interaction, self.pending)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


async def open_menu(
    interaction: discord.Interaction,
    spec: MenuSpec,
    *,
    manage_required: bool = True,
) -> None:
    """Open ``spec`` for the invoking member."""
    if interaction.guild_id is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    if manage_required and not can_manage(interaction.user):
        await interaction.response.send_message(
            "You need **Manage Server** to use this menu.", ephemeral=True
        )
        return

    session = MenuSession(interaction, spec)
    await session.open(interaction)
