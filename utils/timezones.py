"""Timezone input parsing: IANA names and fixed UTC offsets.

Two accepted syntaxes for `/config qotd settz`:

  - IANA names ("Asia/Kolkata", "Europe/London") - DST-aware, handled by
    zoneinfo as before.
  - Fixed offsets ("UTC+5:30", "+5", "-8:30", "0") - parsed here. Simpler
    for servers that do not care about DST, which is most of them.

Offsets are normalised to strings like "UTC+05:30" on save, so the stored
value is always printable and the scheduler can re-parse it every tick.
A fixed offset never shifts for DST - that is the documented price of the
simple input. IANA names remain available for anyone who wants DST.

Lives in utils/ (not services/) because both config_service (validation)
and qotd_scheduler (resolution) need it, and services/qotd_scheduler.py
already imports services/config_service.py - the reverse import would be
circular.
"""

from __future__ import annotations

import re
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from utils.logging_setup import get_logger

log = get_logger("utils.timezones")

# Discord-visible syntax error: the cog catches this and renders an embed
# with a "Google it for me" link button instead of a bare error line.
class TimezoneError(ValueError):
    """The timezone input matched neither the offset nor the IANA syntax."""


# utc? optional, hours 0-14, optional :mm or mm (0-59). The sign is matched
# but NOT optional by policy: bare "5:30" is ambiguous about direction, so
# unsigned input is rejected in code below (except a bare zero, which has no
# direction to be ambiguous about).
_OFFSET_RE = re.compile(
    r"^(?:utc)?\s*([+-])?\s*(\d{1,2})(?::?(\d{2}))?$",
    re.IGNORECASE,
)

# Real-world maximum offset is UTC+14 (Kiribati); -12 is the min.
_MAX_OFFSET = timedelta(hours=14)


def parse_utc_offset(raw: str | None) -> timezone | None:
    """Parse "UTC+5:30" / "+5" / "-8:30" / "0" into a fixed-offset tzinfo.

    Returns None when the input is not offset syntax (the caller then tries
    IANA). Returns None for out-of-range values too, so bad input is never
    silently clamped.
    """
    if not raw:
        return None
    match = _OFFSET_RE.match(str(raw).strip())
    if match is None:
        return None

    sign_text, hours_text, minutes_text = match.groups()
    hours = int(hours_text)
    minutes = int(minutes_text) if minutes_text else 0
    if minutes > 59 or hours > 14:
        return None
    # No sign is only fine for exactly zero ("0", "UTC+0" handled by sign);
    # "+5" needs the sign because bare "5" does not say which side of UTC.
    if sign_text is None and (hours != 0 or minutes != 0):
        return None

    delta = timedelta(hours=hours, minutes=minutes)
    if delta > _MAX_OFFSET:
        return None
    if sign_text == "-":
        delta = -delta
    return timezone(delta)


def format_utc_offset(value: timezone) -> str:
    """Canonical stored form of a fixed offset: 'UTC+05:30', 'UTC-08:00'."""
    total_minutes = int(value.utcoffset(None).total_seconds() // 60)  # type: ignore[union-attr]
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"UTC{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def normalize_timezone_input(raw: str) -> str:
    """Validated canonical string for storage, or TimezoneError.

    'UTC' written by a human becomes 'UTC+00:00' on purpose: the column
    default is the plain string 'UTC', so the scheduler/status can tell
    "never chose a timezone" (nudge applies) from "chose UTC" (no nudge).
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        raise TimezoneError("timezone must not be empty")

    offset = parse_utc_offset(cleaned)
    if offset is not None:
        return format_utc_offset(offset)

    if cleaned.upper() == "UTC":
        return "UTC+00:00"

    try:
        ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimezoneError(
            f"unknown timezone {cleaned!r}. Use an IANA name such as "
            "Asia/Kolkata, or a fixed offset such as UTC+5:30."
        ) from exc
    return cleaned


def resolve_zone(name: str | None) -> tzinfo:
    """Stored timezone string -> tzinfo. Fixed offsets and IANA both work.

    Unknown values fall back to UTC with a logged warning rather than
    raising: a bad row in the DB must never kill the QOTD tick.
    """
    text = (name or "").strip()
    if not text:
        return ZoneInfo("UTC")

    offset = parse_utc_offset(text)
    if offset is not None:
        return offset

    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown timezone %r in storage; falling back to UTC", text)
        return ZoneInfo("UTC")


__all__ = [
    "TimezoneError",
    "parse_utc_offset",
    "format_utc_offset",
    "normalize_timezone_input",
    "resolve_zone",
]
