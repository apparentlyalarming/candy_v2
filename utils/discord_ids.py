"""Discord snowflake ID helpers.

Discord IDs exceed JavaScript's Number.MAX_SAFE_INTEGER (2^53 - 1). Any place
where an ID crosses a JSON boundary, keep it as a string. On the bot side we
work in ``int`` and only stringify at the very edge (Postgres writes are fine
as int; JSON payloads are the risk).

This module is the single place that conversion logic lives so the rule is not
re-implemented (and mis-implemented) in each cog.
"""

from __future__ import annotations

from typing import Any

MAX_SAFE_JS_INT = 2**53 - 1


def to_id(value: Any) -> int | None:
    """Coerce a Discord ID to ``int`` safely.

    Accepts ``int``, ``str``, or objects with an ``id`` attribute (discord.py
    models). Returns ``None`` for ``None``/empty. Never goes through ``float``,
    which is the classic precision-loss bug.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise TypeError("bool is not a valid Discord ID")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError as exc:
            raise TypeError(f"not a valid Discord ID string: {value!r}") from exc

    # discord.py objects (Guild, TextChannel, Role, Member, User, ...)
    inner = getattr(value, "id", None)
    if inner is not None:
        return to_id(inner)

    raise TypeError(f"cannot interpret {type(value).__name__} as a Discord ID")


def to_id_str(value: Any) -> str | None:
    """Coerce a Discord ID to its canonical string form."""
    as_int = to_id(value)
    return None if as_int is None else str(as_int)


def to_id_list(values: Any) -> list[int]:
    """Coerce an iterable of IDs to a sorted, de-duplicated list of ints."""
    if not values:
        return []
    seen: set[int] = set()
    for item in values:
        parsed = to_id(item)
        if parsed is not None:
            seen.add(parsed)
    return sorted(seen)


def is_snowflake(value: Any) -> bool:
    """True when the value looks like a plausible Discord snowflake."""
    try:
        parsed = to_id(value)
    except TypeError:
        return False
    return parsed is not None and parsed > 0


def needs_string_transport(value: Any) -> bool:
    """True when this ID would lose precision as a JS number.

    Useful as a guard before serialising IDs into JSON for the browser.
    """
    parsed = to_id(value)
    return parsed is not None and parsed > MAX_SAFE_JS_INT


__all__ = [
    "MAX_SAFE_JS_INT",
    "to_id",
    "to_id_str",
    "to_id_list",
    "is_snowflake",
    "needs_string_transport",
]
