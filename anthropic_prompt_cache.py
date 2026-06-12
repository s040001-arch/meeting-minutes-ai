"""Helpers for Anthropic prompt caching (ephemeral cache on static system prefix)."""
from __future__ import annotations

from typing import Any

# Opus 4.8 snapshot (replaces claude-opus-4-7 / claude-opus-4-20250514 lineage).
OPUS_MODEL_ID = "claude-opus-4-8"


def cached_system(static_text: str, variable_text: str = "") -> str | list[dict[str, Any]]:
    """Return a system value with cache_control on the fixed prefix.

    Place static instructions first and job-specific context in ``variable_text``.
    The cache breakpoint sits at the end of the static block.
    """
    static = str(static_text or "")
    variable = str(variable_text or "")
    if not static.strip() and not variable.strip():
        return ""
    if not static.strip():
        return variable
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": static,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if variable.strip():
        blocks.append({"type": "text", "text": variable})
    return blocks
