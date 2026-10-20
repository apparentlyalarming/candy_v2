"""Template rendering for fun action replies.

THE CONTRACT (mirrors candy_web Phase 5):
  - Templates are stored with ``{user}`` and ``{target}`` placeholders.
  - Raw ``<@id>`` mentions must NEVER be stored in the database.
  - This module is the ONE renderer. The web preview pane implements the same
    replace rules. Two divergent render paths is the #1 way this feature breaks.
"""

from __future__ import annotations

import random
from typing import Any, Iterable, Sequence

from utils.discord_ids import to_id

# Placeholder tokens
PLACEHOLDER_USER = "{user}"
PLACEHOLDER_TARGET = "{target}"
KNOWN_PLACEHOLDERS = (PLACEHOLDER_USER, PLACEHOLDER_TARGET)

# Discord's hard message cap is 2000 characters; keep individual templates much
# shorter so a bad list can never blow up a send.
MAX_TEMPLATE_LENGTH = 200
MAX_TEMPLATES_PER_ACTION = 100


class TemplateError(ValueError):
    """Raised when a template or template list is invalid."""


def render_template(template: str, author_id: Any, target_id: Any) -> str:
    """Substitute ``{user}`` and ``{target}`` with Discord mention strings.

    Returns the rendered text. Raises :class:`TemplateError` if an ID cannot be
    resolved, so a bad ID surfaces as a clear error instead of ``<@None>``.
    """
    if not isinstance(template, str):
        raise TemplateError(f"template must be a string, got {type(template).__name__}")

    author = to_id(author_id)
    target = to_id(target_id)
    if author is None:
        raise TemplateError("author_id is required to render a template")
    if target is None:
        raise TemplateError("target_id is required to render a template")

    return (
        template.replace(PLACEHOLDER_USER, f"<@{author}>")
        .replace(PLACEHOLDER_TARGET, f"<@{target}>")
    )


def render_random(templates: Sequence[str], author_id: Any, target_id: Any) -> str:
    """Pick a template at random and render it."""
    if not templates:
        raise TemplateError("no templates available to render")
    return render_template(random.choice(list(templates)), author_id, target_id)


def validate_template(template: str) -> str:
    """Validate a single template. Returns it stripped, or raises.

    Rules (enforced identically in the web editor):
      - non-empty
      - within MAX_TEMPLATE_LENGTH
      - contains at least one known placeholder
      - does not contain a raw ``<@...>`` mention
    """
    if not isinstance(template, str):
        raise TemplateError(f"template must be a string, got {type(template).__name__}")

    cleaned = template.strip()
    if not cleaned:
        raise TemplateError("template must not be empty")

    if len(cleaned) > MAX_TEMPLATE_LENGTH:
        raise TemplateError(
            f"template is too long ({len(cleaned)} chars, max {MAX_TEMPLATE_LENGTH})"
        )

    if not any(token in cleaned for token in KNOWN_PLACEHOLDERS):
        raise TemplateError(
            f"template must contain at least one of {', '.join(KNOWN_PLACEHOLDERS)}"
        )

    if "<@" in cleaned:
        raise TemplateError(
            "template must not contain a raw <@...> mention; "
            f"use {PLACEHOLDER_USER} and {PLACEHOLDER_TARGET} instead"
        )

    return cleaned


def validate_template_list(templates: Iterable[str]) -> list[str]:
    """Validate and normalise a whole list. Returns a clean list, or raises.

    Duplicates are rejected (they make the random pick look broken to users).
    """
    if isinstance(templates, str):
        raise TemplateError("templates must be a list, not a single string")

    cleaned: list[str] = []
    seen: set[str] = set()

    for index, template in enumerate(templates):
        try:
            value = validate_template(template)
        except TemplateError as exc:
            raise TemplateError(f"template #{index + 1}: {exc}") from exc

        key = value.casefold()
        if key in seen:
            raise TemplateError(f"template #{index + 1}: duplicate of an earlier template")
        seen.add(key)
        cleaned.append(value)

    if len(cleaned) > MAX_TEMPLATES_PER_ACTION:
        raise TemplateError(
            f"too many templates ({len(cleaned)}, max {MAX_TEMPLATES_PER_ACTION})"
        )

    return cleaned


def preview(templates: Sequence[str], author_name: str = "You", target_name: str = "Them") -> list[str]:
    """Human-readable preview used by /config replies list.

    Replaces mentions with display names so the admin sees prose rather than
    raw mention syntax.
    """
    out: list[str] = []
    for template in templates:
        if not isinstance(template, str):
            continue
        out.append(
            template.replace(PLACEHOLDER_USER, f"@{author_name}").replace(
                PLACEHOLDER_TARGET, f"@{target_name}"
            )
        )
    return out


__all__ = [
    "PLACEHOLDER_USER",
    "PLACEHOLDER_TARGET",
    "KNOWN_PLACEHOLDERS",
    "MAX_TEMPLATE_LENGTH",
    "MAX_TEMPLATES_PER_ACTION",
    "TemplateError",
    "render_template",
    "render_random",
    "validate_template",
    "validate_template_list",
    "preview",
]
