"""Layer 3: BEFORE/AFTER fact integrity verification (Phase 10).

Shared by contextual_editor apply path and verify scripts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_FLAGGED_TOKEN_RE = re.compile(r"[^\s\n。、]{1,40}\[要確認\]")
_HEADING_RE = re.compile(r"^(?:###\s*)?▼")
# Substantive amounts — bare digits in garble (e.g. 「16時にちょっ」) are not protected.
_AMOUNT_PROTECT_RE = re.compile(
    r"(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?:万円|万|円|%|倍)"
)
# Schedule / headcount facts (substantive; not garble-internal lone digits).
_SCHEDULE_PROTECT_RE = re.compile(r"\d+\s*(?:年目|クラス|月|人)")


@dataclass
class FactSnapshot:
    amounts: set[str] = field(default_factory=set)
    schedule_tokens: set[str] = field(default_factory=set)
    flagged_tokens: list[str] = field(default_factory=list)
    participant_mentions: set[str] = field(default_factory=set)
    place_mentions: set[str] = field(default_factory=set)
    heading_count: int = 0


@dataclass
class GateResult:
    ok: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _extract_amounts(text: str) -> set[str]:
    found: set[str] = set()
    for m in _AMOUNT_PROTECT_RE.finditer(text or ""):
        token = m.group(0).strip()
        if token:
            found.add(token)
    return found


def _extract_schedule_tokens(text: str) -> set[str]:
    found: set[str] = set()
    for m in _SCHEDULE_PROTECT_RE.finditer(text or ""):
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


def _place_mentions(text: str, places: list[str]) -> set[str]:
    mentioned: set[str] = set()
    for name in places:
        n = str(name).strip()
        if n and len(n) >= 2 and n in text:
            mentioned.add(n)
    return mentioned


def extract_fact_snapshot(
    text: str,
    *,
    meeting_profile: dict[str, Any] | None = None,
    extra_place_names: list[str] | None = None,
) -> FactSnapshot:
    profile = meeting_profile or {}
    participants = []
    for key in ("participants", "attendees", "customer_names"):
        participants.extend(str(x).strip() for x in (profile.get(key) or []) if str(x).strip())

    places = list(extra_place_names or [])
    for memo in profile.get("relevant_knowledge") or []:
        for token in str(memo).replace("、", " ").split():
            t = token.strip()
            if len(t) >= 2:
                places.append(t)

    heading_count = sum(1 for line in (text or "").splitlines() if _HEADING_RE.match(line.strip()))
    return FactSnapshot(
        amounts=_extract_amounts(text),
        schedule_tokens=_extract_schedule_tokens(text),
        flagged_tokens=_extract_flagged_tokens(text),
        participant_mentions=_participant_mentions(text, participants),
        place_mentions=_place_mentions(text, places),
        heading_count=heading_count,
    )


def _compare_token_sets(
    violations: list[str],
    *,
    label: str,
    before: set[str],
    after: set[str],
) -> None:
    missing = before - after
    if missing:
        violations.append(f"{label}_missing:{sorted(missing)}")
    added = after - before
    if added:
        violations.append(f"{label}_added:{sorted(added)}")


def verify_fact_integrity(
    before: str,
    after: str,
    *,
    meeting_profile: dict[str, Any] | None = None,
    extra_place_names: list[str] | None = None,
) -> GateResult:
    snap_before = extract_fact_snapshot(
        before, meeting_profile=meeting_profile, extra_place_names=extra_place_names
    )
    snap_after = extract_fact_snapshot(
        after, meeting_profile=meeting_profile, extra_place_names=extra_place_names
    )
    violations: list[str] = []
    warnings: list[str] = []

    _compare_token_sets(
        violations, label="amounts", before=snap_before.amounts, after=snap_after.amounts
    )
    _compare_token_sets(
        violations,
        label="schedule",
        before=snap_before.schedule_tokens,
        after=snap_after.schedule_tokens,
    )

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

    _compare_token_sets(
        violations,
        label="participant",
        before=snap_before.participant_mentions,
        after=snap_after.participant_mentions,
    )
    _compare_token_sets(
        violations,
        label="place",
        before=snap_before.place_mentions,
        after=snap_after.place_mentions,
    )

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
