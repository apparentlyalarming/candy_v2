"""Supabase client wiring.

The bot uses the SERVICE ROLE key. It bypasses RLS by design: the bot is a
trusted server-side actor, exactly like the sync-guilds Edge Function. The key
must never appear in the frontend repo or in git.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from supabase import Client, create_client

from config import settings
from utils.logging_setup import get_logger

log = get_logger("db.supabase")


@lru_cache(maxsize=1)
def get_client() -> Client:
    """Return the shared Supabase client (created once per process)."""
    if not settings.supabase_url or not settings.supabase_service_key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY are not set. "
            "The bot cannot talk to the database without them."
        )
    log.info("Creating Supabase client for %s", settings.supabase_url)
    return create_client(settings.supabase_url, settings.supabase_service_key)


# --- thin helpers -----------------------------------------------------------
# Centralised so error handling and logging are consistent everywhere.


def select(
    table: str,
    columns: str = "*",
    *,
    filters: dict[str, Any] | None = None,
    order_by: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """SELECT rows, returning a list of dicts. Never returns None."""
    query = get_client().table(table).select(columns)
    for column, value in (filters or {}).items():
        if value is None:
            query = query.is_(column, "null")
        else:
            query = query.eq(column, value)
    if order_by:
        query = query.order(order_by)
    if limit is not None:
        query = query.limit(limit)

    response = query.execute()
    return list(response.data or [])


def select_one(
    table: str, columns: str = "*", *, filters: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """SELECT at most one row."""
    rows = select(table, columns, filters=filters, limit=1)
    return rows[0] if rows else None


def upsert(table: str, payload: dict[str, Any], *, on_conflict: str | None = None) -> list[dict[str, Any]]:
    """UPSERT one or many rows. Returns the written rows."""
    query = get_client().table(table).upsert(payload, on_conflict=on_conflict)
    response = query.execute()
    return list(response.data or [])


def update(
    table: str, payload: dict[str, Any], *, filters: dict[str, Any]
) -> list[dict[str, Any]]:
    """UPDATE matching rows. ``filters`` must be non-empty to avoid a mass update."""
    if not filters:
        raise ValueError("update() requires at least one filter")
    query = get_client().table(table).update(payload)
    for column, value in filters.items():
        query = query.eq(column, value)
    response = query.execute()
    return list(response.data or [])


def delete(table: str, *, filters: dict[str, Any]) -> list[dict[str, Any]]:
    """DELETE matching rows. ``filters`` must be non-empty."""
    if not filters:
        raise ValueError("delete() requires at least one filter")
    query = get_client().table(table).delete()
    for column, value in filters.items():
        query = query.eq(column, value)
    response = query.execute()
    return list(response.data or [])


def call_rpc(function: str, params: dict[str, Any]) -> Any:
    """Invoke a Postgres function (used for the atomic QOTD queue helpers).

    The bot runs as service_role, so ``can_manage_guild()`` inside those
    functions will evaluate ``auth.uid()`` as NULL. The functions are
    ``security invoker`` and raise 42501 when unauthorized, so the bot skips
    them and writes directly instead. See ConfigService for that path.
    """
    response = get_client().rpc(function, params).execute()
    return response.data


__all__ = [
    "get_client",
    "select",
    "select_one",
    "upsert",
    "update",
    "delete",
    "call_rpc",
]
