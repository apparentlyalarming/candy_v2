"""candy_v2 — Discord bot package."""

__version__ = "0.1.0"

from typing import Any


def bind_group_commands(cog: Any) -> int:
    """Bind this cog's app-command callbacks to the cog instance.

    WHY THIS IS NEEDED
        discord.py gives a subcommand its ``binding`` in Group.__init__, which
        copies each child with ``binding=self`` - the GROUP, not the cog. For a
        group declared INSIDE a Cog body, the metaclass then re-copies the group
        with the real cog instance (via __set_name__ / _owner_cls), so binding
        ends up correct.

        candy_v2 declares its groups at MODULE level so they can be shared and
        parented (config_group, general_group, chat_group, ...). Those groups
        never go through that re-copy, so every child keeps binding=None.

        At invocation time Command._do_call does:

            await self._callback(self.binding, interaction, **params)

        With binding=None that passes the interaction into the function's
        ``self`` slot, so the real ``interaction`` parameter has no value and
        the call raises TypeError. discord.py catches that and reports it as:

            CommandSignatureMismatch: The signature for command 'x' is
            different from the one provided by Discord.

        ...which is misleading: the command signature is fine, the BINDING is
        missing. This helper repairs it after the cog is added.

    Returns the number of commands rebound, for logging.
    """
    tree = cog.bot.tree
    cog_cls_qualname = type(cog).__name__ + "."
    rebound = 0
    for command in tree.walk_commands():
        callback = getattr(command, "_callback", None)
        if callback is None:
            # A Group node; only leaf Commands carry a callback.
            continue
        if getattr(command, "binding", None) is not None:
            continue
        # Only claim callbacks that actually live on this cog class.
        if not callback.__qualname__.startswith(cog_cls_qualname):
            continue
        command.binding = cog
        rebound += 1
    return rebound

