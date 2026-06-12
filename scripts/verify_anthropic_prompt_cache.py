#!/usr/bin/env python3
"""Verify Anthropic prompt caching returns cache_* usage fields (read-only API call)."""
from __future__ import annotations

import json
import os
import sys

import anthropic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from anthropic_prompt_cache import OPUS_MODEL_ID, cached_system  # noqa: E402
from repo_env import load_dotenv_local  # noqa: E402


def main() -> int:
    load_dotenv_local()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print(json.dumps({"skipped": True, "reason": "ANTHROPIC_API_KEY not set"}, ensure_ascii=False))
        return 0

    static = "You are a test assistant. Reply with exactly: OK\n" + ("x" * 1100)
    client = anthropic.Anthropic(api_key=api_key)
    resp1 = client.messages.create(
        model=OPUS_MODEL_ID,
        max_tokens=16,
        system=cached_system(static),
        messages=[{"role": "user", "content": "ping-1"}],
    )
    resp2 = client.messages.create(
        model=OPUS_MODEL_ID,
        max_tokens=16,
        system=cached_system(static),
        messages=[{"role": "user", "content": "ping-2"}],
    )
    u1 = getattr(resp1, "usage", None)
    u2 = getattr(resp2, "usage", None)
    out = {
        "model": OPUS_MODEL_ID,
        "call1": {
            "cache_creation_input_tokens": getattr(u1, "cache_creation_input_tokens", None),
            "cache_read_input_tokens": getattr(u1, "cache_read_input_tokens", None),
            "input_tokens": getattr(u1, "input_tokens", None),
        },
        "call2": {
            "cache_creation_input_tokens": getattr(u2, "cache_creation_input_tokens", None),
            "cache_read_input_tokens": getattr(u2, "cache_read_input_tokens", None),
            "input_tokens": getattr(u2, "input_tokens", None),
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
