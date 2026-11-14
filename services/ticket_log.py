"""Ticket transcript summaries for the ``!log`` command.

A ticket channel (name starting with ``ticket``) can be exported as a small
JSON document summarising who was involved, when it happened, why, and what
kind of ticket it was. The summary is produced by the same ``chat`` Edge
Function as every other AI feature - the Groq key stays in Supabase secrets,
and the bot never talks to Groq directly.

Why the transcript is chunked
    The Edge Function caps each message at 6000 characters and accepts at most
    40 messages per request. A 1000-message ticket does not fit in one message,
    so the transcript is split on line boundaries into several user messages.
    The model reads all of them before answering.

Why the JSON is stored as well as returned
    The file is the human-facing artefact; the row in ``guild_ticket_logs`` is
    what the monthly report counts and what the dashboard can query later.
    Both come from the same parsed object, so they cannot disagree.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from db import supabase_client
from services.groq_service import GroqService
from utils.logging_setup import get_logger

log = get_logger("services.ticket_log")

# The command reads at most this many messages before summarising.
MAX_HISTORY_MESSAGES = 1000

# --- Edge Function limits (see supabase/functions/chat/lib.ts) ---------------
MAX_MESSAGE_CHARS = 6000
MAX_MESSAGES = 40
# The system prompt takes one slot and the "split across N messages" note takes
# another, leaving this many chunks for the transcript itself.
MAX_USER_CHUNKS = MAX_MESSAGES - 2

# ``llama3-8b-8192`` was decommissioned by Groq on 2025-08-30. This is the
# model Groq's deprecation notice names as its replacement.
SUMMARY_MODEL = "llama-3.1-8b-instant"
SUMMARY_MAX_TOKENS = 1024
SUMMARY_TEMPERATURE = 0.2

# The ticket category is not encoded in the channel name (they are all
# ``ticket-0001``), so the model has to read it from the conversation. These are
# the labels the dashboard and the monthly report group by; a model that answers
# with a synonym is folded onto one of them instead of creating a new bucket.
TICKET_TYPE_OTHER = "Other"
TICKET_TYPE_ORDER = (
    "General support",
    "Reporting a user",
    "Partnership request",
    "Custom role",
    "Staff application",
    "Other",
)

_TICKET_TYPE_ALIASES = {
    "general support": "General support",
    "general support ticket": "General support",
    "general": "General support",
    "support": "General support",
    "reporting a user": "Reporting a user",
    "report a user": "Reporting a user",
    "reporting user": "Reporting a user",
    "user report": "Reporting a user",
    "member report": "Reporting a user",
    "report": "Reporting a user",
    "partnership request": "Partnership request",
    "partnership": "Partnership request",
    "partner request": "Partnership request",
    "custom role": "Custom role",
    "custom role request": "Custom role",
    "role request": "Custom role",
    "staff application": "Staff application",
    "staff app": "Staff application",
    "application": "Staff application",
    "other": TICKET_TYPE_OTHER,
    "unknown": TICKET_TYPE_OTHER,
}

SYSTEM_PROMPT = (
    "You summarise Discord ticket transcripts for a server's staff log. "
    "Read the transcript and reply with a single JSON object and nothing else. "
    "It must have exactly these four keys:\n"
    '- "people_involved": an array of usernames of everyone who took part, '
    "including staff\n"
    '- "when": a string with the start and end of the conversation, formatted '
    'as "YYYY-MM-DD HH:MM to YYYY-MM-DD HH:MM" (transcript timestamps are UTC)\n'
    '- "why": a short factual summary of why the ticket was opened and how it '
    "was resolved\n"
    '- "ticket_type": the kind of ticket, exactly one of "General support", '
    '"Reporting a user", "Partnership request", "Custom role", '
    '"Staff application", or "Other". Infer it from what the user asked for; '
    'use "Other" when the conversation does not make it clear.\n'
    'A line starting with "Ticket form" is the ticket bot\'s opening embed: it '
    "often states the chosen type. Use it for ticket_type, but never list the "
    "ticket bot in people_involved.\n"
    "Keep ticket_type to one of those labels - do not invent a new category. "
    "Do not add any other keys. Do not wrap the JSON in markdown fences. "
    "Do not write anything outside the JSON."
)


class TicketLogError(ValueError):
    """A ticket transcript could not be summarised or recorded."""


@dataclass(frozen=True)
class Transcript:
    """Formatted transcript text plus the number of human messages in it."""

    text: str
    message_count: int


# ---------------------------------------------------------------------------
# transcript building
# ---------------------------------------------------------------------------


def _format_embeds(embeds: list[discord.Embed]) -> str:
    """Compact text for embeds, used to surface a ticket bot's opening form."""
    parts: list[str] = []
    for embed in embeds:
        if embed.author and embed.author.name:
            parts.append(embed.author.name)
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(" ".join(embed.description.split()))
        for field in embed.fields:
            name = " ".join((field.name or "").split())
            value = " ".join((field.value or "").split())
            if name or value:
                parts.append(f"{name}: {value}" if name else value)
        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)
    return " | ".join(part for part in parts if part)


def format_message(message: discord.Message) -> str:
    """Format one message as ``AuthorName (YYYY-MM-DD HH:MM): Content``.

    A bot message is only formatted when it carries embeds, and then without an
    author name: ticket bots put the selected ticket type in their opening
    embed, and that is often the clearest signal for ``ticket_type``. Labelling
    it ``Ticket form`` keeps the bot out of ``people_involved``.
    """
    stamp = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if message.author.bot:
        form = _format_embeds(list(message.embeds))
        return f"Ticket form ({stamp}): {form}" if form else ""

    author = getattr(message.author, "display_name", None) or message.author.name
    content = " ".join((message.content or "").split())
    if not content:
        if message.attachments:
            content = "[attachment]"
        elif message.embeds:
            content = "[embed]"
        else:
            content = "[no text]"
    return f"{author} ({stamp}): {content}"


async def fetch_transcript(
    channel: discord.TextChannel | discord.Thread,
) -> Transcript:
    """Read up to :data:`MAX_HISTORY_MESSAGES` human messages, oldest first.

    Ordinary bot messages are ignored. A bot message with embeds is kept as
    ticket-form context (see :func:`format_message`), but only human messages
    are counted in :attr:`Transcript.message_count`.
    """
    lines: list[str] = []
    human_messages = 0
    async for message in channel.history(
        limit=MAX_HISTORY_MESSAGES, oldest_first=True
    ):
        if message.author.bot and not message.embeds:
            continue
        line = format_message(message)
        if line:
            lines.append(line)
        if not message.author.bot:
            human_messages += 1
    return Transcript(text="\n".join(lines), message_count=human_messages)


def chunk_transcript(transcript: str) -> list[str]:
    """Split a transcript into messages the Edge Function will accept.

    Splits on line boundaries where possible so a single message is not cut in
    half. A line longer than the limit is hard-split. If the result is still
    too large, the middle is dropped and the beginning and end are kept, since
    the opening describes the problem and the end usually describes the fix.
    """
    chunks: list[str] = []
    current = ""

    for line in transcript.splitlines():
        while len(line) > MAX_MESSAGE_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:MAX_MESSAGE_CHARS])
            line = line[MAX_MESSAGE_CHARS:]

        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= MAX_MESSAGE_CHARS:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line

    if current:
        chunks.append(current)

    if len(chunks) > MAX_USER_CHUNKS:
        keep = MAX_USER_CHUNKS - 1
        head = keep // 2
        tail = keep - head
        log.warning(
            "Ticket transcript is %d chunks; trimming to the first %d and last %d",
            len(chunks),
            head,
            tail,
        )
        chunks = [
            *chunks[:head],
            "[... middle of the transcript omitted ...]",
            *chunks[-tail:],
        ]

    return chunks


def build_messages(transcript: str) -> list[dict[str, str]]:
    """Turn a transcript into the Edge Function's message array."""
    chunks = chunk_transcript(transcript)
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if len(chunks) > 1:
        messages.append(
            {
                "role": "user",
                "content": (
                    f"The transcript is split across {len(chunks)} messages. "
                    "Read all of them before answering."
                ),
            }
        )
    messages.extend({"role": "user", "content": chunk} for chunk in chunks)
    return messages


# ---------------------------------------------------------------------------
# summary parsing / rendering
# ---------------------------------------------------------------------------


def parse_summary(raw: str) -> dict[str, object]:
    """Parse the model's reply into exactly the four required keys.

    Extra keys are dropped, missing ones become empty values, and the ticket
    type is folded onto a known label, so a chatty model cannot change the
    shape of the file or the report buckets.
    """
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    data: object
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Some models prepend a sentence. Grab the outermost braces.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise TicketLogError("The AI did not return a JSON object.") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise TicketLogError("The AI returned invalid JSON.") from exc

    if not isinstance(data, dict):
        raise TicketLogError("The AI returned JSON that is not an object.")

    people = data.get("people_involved")
    if isinstance(people, str):
        people = [people]
    if not isinstance(people, list):
        people = []
    # Preserve order but drop blanks/duplicates so the file reads cleanly.
    cleaned: list[str] = []
    seen: set[str] = set()
    for person in people:
        name = str(person).strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            cleaned.append(name)

    when = data.get("when")
    why = data.get("why")

    return {
        "people_involved": cleaned,
        "when": str(when).strip() if when is not None else "",
        "why": str(why).strip() if why is not None else "",
        "ticket_type": normalize_ticket_type(data.get("ticket_type")),
    }


def normalize_ticket_type(value: object) -> str:
    """Fold the model's answer onto one of :data:`TICKET_TYPE_ORDER`.

    The model is told the exact labels, but small variations ("report user",
    "partnership") must not create new report buckets. Anything unrecognised
    becomes ``Other`` rather than leaking free text into the monthly report.
    """
    text = " ".join(str(value or "").split()).casefold()
    text = " ".join(text.replace("_", " ").replace("-", " ").split())
    if text in _TICKET_TYPE_ALIASES:
        return _TICKET_TYPE_ALIASES[text]

    for label in TICKET_TYPE_ORDER:
        key = label.casefold()
        if text and (key in text or text in key):
            return label
    return TICKET_TYPE_OTHER


def render_log_json(summary: dict[str, object]) -> str:
    """Render the summary exactly as it is sent to Discord."""
    return json.dumps(summary, indent=4, ensure_ascii=False)


def filename_for(
    channel_name: str,
    ticket_type: str | None = None,
    *,
    when: datetime | None = None,
) -> str:
    """A safe, distinguishable attachment name.

    Channel names repeat across ticket types - every type has its own
    ``ticket-0001`` - so the channel name alone produces colliding files. The
    inferred type and the export date keep them apart::

        ticket-0001_2026-11-14_general-support_log.json
    """
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", channel_name or "").strip("._")
    parts = [base or "ticket"]
    parts.append((when or datetime.now(timezone.utc)).strftime("%Y-%m-%d"))
    if ticket_type:
        slug = re.sub(r"[^a-z0-9]+", "-", ticket_type.lower()).strip("-")
        parts.append(slug or "other")
    return "_".join(parts) + "_log.json"


# ---------------------------------------------------------------------------
# Groq + database
# ---------------------------------------------------------------------------


async def summarize_ticket(groq: GroqService, *, transcript: str) -> dict[str, object]:
    """Ask the AI for the four-key summary and validate its shape."""
    raw = await groq.complete_json(
        messages=build_messages(transcript),
        model=SUMMARY_MODEL,
        max_tokens=SUMMARY_MAX_TOKENS,
        temperature=SUMMARY_TEMPERATURE,
    )
    return parse_summary(raw)


async def record_ticket_log(
    *,
    guild_id: int,
    channel_id: int,
    channel_name: str,
    generated_by: int,
    generated_by_name: str,
    message_count: int,
    summary: dict[str, object],
) -> None:
    """Persist the summary JSON for the monthly report and the dashboard.

    ``ticket_type`` is duplicated out of ``summary`` into its own column so the
    report can group by it without digging through JSONB.
    """
    payload = {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "generated_by": generated_by,
        "generated_by_name": generated_by_name,
        "message_count": message_count,
        "ticket_type": normalize_ticket_type(summary.get("ticket_type")),
        "summary": summary,
    }
    await asyncio.to_thread(supabase_client.insert, "guild_ticket_logs", payload)


__all__ = [
    "MAX_HISTORY_MESSAGES",
    "SYSTEM_PROMPT",
    "SUMMARY_MODEL",
    "TICKET_TYPE_ORDER",
    "TICKET_TYPE_OTHER",
    "TicketLogError",
    "Transcript",
    "build_messages",
    "chunk_transcript",
    "fetch_transcript",
    "filename_for",
    "format_message",
    "normalize_ticket_type",
    "parse_summary",
    "record_ticket_log",
    "render_log_json",
    "summarize_ticket",
]
