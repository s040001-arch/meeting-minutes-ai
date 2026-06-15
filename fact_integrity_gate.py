"""Layer 3: BEFORE/AFTER fact integrity verification (Phase 10).

Shared by contextual_editor apply path and verify scripts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_FLAGGED_TOKEN_RE = re.compile(r"[^\s\n。、]{1,40}\[要確認\]")
_HEADING_RE = re.compile(r"^(?:###\s*)?▼")
# Substantive amounts only — bare digits in garble (e.g. 「16時にちょっ」) are not protected.
_NUMERIC_NORMALIZE_RE = re.compile(
    r"(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?:万円|万|円|%|倍|人|名|件)"
)


@dataclass
class FactSnapshot:
    numbers: set[str] = field(default_factory=set)
    flagged_tokens: list[str] = field(default_factory=list)
    participant_mentions: set[str] = field(default_factory=set)
    heading_count: int = 0


@dataclass
class GateResult:
    ok: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _extract_numbers(text: str) -> set[str]:
    found: set[str] = set()
    for m in _NUMERIC_NORMALIZE_RE.finditer(text or ""):
        token = m.group(0).strip()
        if token:
            found.add(token)
    return found


def _extract_flagged_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for m in _FLAGGED_TOKEN_RE.finditer(text or ""):
        left = text[max(0, m.start() - 80) : m.start()]
        frag = re.split(r"[\n。]", left)[-1]
        tokens.append(f"{frag}[要確認]")
    return tokens


def _participant_mentions(text: str, participants: list[str]) -> set[str]:
    mentioned: set[str] = set()
    for name in participants:
        n = str(name).strip()
        if n and n in text:
            mentioned.add(n)
    return mentioned


def extract_fact_snapshot(
    text: str,
    *,
    meeting_profile: dict[str, Any] | None = None,
) -> FactSnapshot:
    profile = meeting_profile or {}
    participants = []
    for key in ("participants", "attendees", "customer_names"):
        participants.extend(str(x).strip() for x in (profile.get(key) or []) if str(x).strip())

    heading_count = sum(1 for line in (text or "").splitlines() if _HEADING_RE.match(line.strip()))
    return FactSnapshot(
        numbers=_extract_numbers(text),
        flagged_tokens=_extract_flagged_tokens(text),
        participant_mentions=_participant_mentions(text, participants),
        heading_count=heading_count,
    )


def verify_fact_integrity(
    before: str,
    after: str,
    *,
    meeting_profile: dict[str, Any] | None = None,
) -> GateResult:
    snap_before = extract_fact_snapshot(before, meeting_profile=meeting_profile)
    snap_after = extract_fact_snapshot(after, meeting_profile=meeting_profile)
    violations: list[str] = []
    warnings: list[str] = []

    missing_numbers = snap_before.numbers - snap_after.numbers
    if missing_numbers:
        violations.append(f"numbers_missing:{sorted(missing_numbers)}")

    new_numbers = snap_after.numbers - snap_before.numbers
    if new_numbers:
        violations.append(f"numbers_added:{sorted(new_numbers)}")

    if len(snap_after.flagged_tokens) < len(snap_before.flagged_tokens):
        violations.append(
            f"flagged_tokens_decreased:{len(snap_before.flagged_tokens)}->{len(snap_after.flagged_tokens)}"
        )
    if len(snap_after.flagged_tokens) > len(snap_before.flagged_tokens):
        violations.append(
            f"flagged_tokens_increased:{len(snap_before.flagged_tokens)}->{len(snap_after.flagged_tokens)}"
        )
    for token in snap_before.flagged_tokens:
        if token not in after:
            core = token.split("。")[-1]
            if core not in after:
                violations.append(f"flagged_token_missing:{token[:40]}")

    missing_participants = snap_before.participant_mentions - snap_after.participant_mentions
    if missing_participants:
        violations.append(f"participant_missing:{sorted(missing_participants)}")

    if snap_before.heading_count > 0 and snap_after.heading_count < snap_before.heading_count:
        violations.append(
            f"headings_decreased:{snap_before.heading_count}->{snap_after.heading_count}"
        )

    return GateResult(ok=not violations, violations=violations, warnings=warnings)


def simulate_apply_proposals(
    text: str,
    proposals: list[dict[str, Any]],
) -> str:
    """Apply auto_correct and auto_delete proposals for gate simulation (offset-safe)."""
    from edit_proposal_schema import VERDICT_AUTO_CORRECT, VERDICT_AUTO_DELETE

    applicable = [
        p
        for p in proposals
        if str(p.get("verdict") or "") in (VERDICT_AUTO_CORRECT, VERDICT_AUTO_DELETE)
        and int(p.get("span_start") or -1) >= 0
    ]
    ranked = sorted(applicable, key=lambda p: int(p.get("span_start") or 0), reverse=True)
    out = text
    for p in ranked:
        start = int(p["span_start"])
        span_before = str(p.get("span_before") or "")
        if not span_before or out[start : start + len(span_before)] != span_before:
            continue
        end = start + len(span_before)
        if p.get("verdict") == VERDICT_AUTO_DELETE:
            out = out[:start] + out[end:]
        else:
            span_after = str(p.get("span_after") or "")
            out = out[:start] + span_after + out[end:]
    return out
