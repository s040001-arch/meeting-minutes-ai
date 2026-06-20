"""Apply contextual editor proposals (Phase 10.2) with layer-3 + 3b gates."""
from __future__ import annotations

import re
from typing import Any

from edit_proposal_schema import VERDICT_AUTO_CORRECT, VERDICT_AUTO_DELETE
from fact_integrity_gate import verify_fact_integrity

OUTPUT_AI_TRANSCRIPT = "merged_transcript_ai.txt"
EDITOR_APPLY_REPORT_FILENAME = "editor_apply_report.json"

_ADJACENT_PUNCT_RE = re.compile(r"[、。]{2,}")
_LEADING_LINE_PUNCT_RE = re.compile(r"^[、。]+")


def normalize_editor_delete_punctuation(text: str) -> str:
    """Apply-only pass: clean up punctuation left adjacent by ④ deletions.

    ④ removes a span between two punctuation marks (or between a line start
    and a punctuation mark), which can leave 、。/。、/、、/。。 pairs or a
    stray leading 、/。 at the start of a transcript line. This is specific
    to deletion artifacts, so it does not share rules with mechanical's
    cleanup_common_noise (which targets recognition noise, not apply output).
    """
    lines = text.split("\n")
    normalized = []
    for line in lines:
        line = _ADJACENT_PUNCT_RE.sub(lambda m: m.group(0)[-1], line)
        line = _LEADING_LINE_PUNCT_RE.sub("", line)
        normalized.append(line)
    return "\n".join(normalized)


def apply_single_proposal(text: str, proposal: dict[str, Any]) -> str | None:
    """Apply one proposal at span_start. Returns None if span mismatch."""
    start = int(proposal.get("span_start") or -1)
    span_before = str(proposal.get("span_before") or "")
    if start < 0 or not span_before:
        return None
    if text[start : start + len(span_before)] != span_before:
        return None
    end = start + len(span_before)
    verdict = str(proposal.get("verdict") or "")
    if verdict == VERDICT_AUTO_DELETE:
        return text[:start] + text[end:]
    if verdict == VERDICT_AUTO_CORRECT:
        span_after = str(proposal.get("span_after") or "")
        return text[:start] + span_after + text[end:]
    return None


def apply_proposals_with_gate(
    text: str,
    proposals: list[dict[str, Any]],
    *,
    meeting_profile: dict[str, Any] | None = None,
    run_semantic: bool = False,
    sync_garble_span: bool = True,
    skip_structural_semantic: bool = False,
    api_key: str | None = None,
) -> tuple[str, list[dict], list[dict], list[dict], list[dict]]:
    """Apply ①④ in reverse offset order; revert on fact or semantic gate fail.

    Returns (output_text, applied, reverted_fact, skipped, reverted_semantic).
    """
    from semantic_integrity_gate import (
        is_semantic_integrity_gate_enabled,
        sync_delete_span_to_garble_fragment,
        verify_proposal_semantic_step,
    )

    semantic_on = run_semantic or is_semantic_integrity_gate_enabled()

    applicable = [
        p
        for p in proposals
        if str(p.get("verdict") or "") == VERDICT_AUTO_DELETE
        or (
            str(p.get("verdict") or "") == VERDICT_AUTO_CORRECT
            and str(p.get("span_after") or "").strip()
        )
    ]
    ranked = sorted(
        applicable,
        key=lambda p: int(p.get("span_start") or -1),
        reverse=True,
    )

    original = text
    out = text
    applied: list[dict[str, Any]] = []
    reverted_fact: list[dict[str, Any]] = []
    reverted_semantic: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for proposal in ranked:
        start = int(proposal.get("span_start") or -1)
        if start < 0:
            proposal["applied"] = False
            proposal["apply_error"] = "span_unresolved"
            skipped.append(proposal)
            continue

        if sync_garble_span and str(proposal.get("verdict") or "") == VERDICT_AUTO_DELETE:
            sync_delete_span_to_garble_fragment(proposal, out)

        before_step = out
        trial = apply_single_proposal(out, proposal)
        if trial is None:
            proposal["applied"] = False
            proposal["apply_error"] = "span_mismatch"
            skipped.append(proposal)
            continue

        fact_gate = verify_fact_integrity(original, trial, meeting_profile=meeting_profile)
        if not fact_gate.ok:
            proposal["applied"] = False
            proposal["apply_error"] = fact_gate.violations
            proposal["apply_reverted"] = True
            proposal["revert_layer"] = "fact"
            reverted_fact.append(proposal)
            continue

        if semantic_on:
            sem = verify_proposal_semantic_step(
                before_step,
                trial,
                proposal,
                api_key=api_key,
                skip_structural=skip_structural_semantic,
            )
            proposal["semantic_check"] = {
                "ok": sem.ok,
                "issue": sem.issue,
                "reason": sem.reason,
                "source": sem.source,
            }
            if not sem.ok:
                proposal["applied"] = False
                proposal["apply_error"] = sem.reason
                proposal["apply_reverted"] = True
                proposal["revert_layer"] = "semantic"
                reverted_semantic.append(proposal)
                continue

        out = trial
        proposal["applied"] = True
        proposal["apply_error"] = None
        applied.append(proposal)

    out = normalize_editor_delete_punctuation(out)

    return out, applied, reverted_fact, skipped, reverted_semantic
