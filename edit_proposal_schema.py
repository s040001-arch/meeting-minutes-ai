"""Edit proposal schema (Phase 10 contextual editor).

Dual-read with coherence anomalies and unknown_points.
"""
from __future__ import annotations

import uuid
from typing import Any

VERDICT_AUTO_CORRECT = "auto_correct"
VERDICT_ASK_WITH_CANDIDATE = "ask_with_candidate"
VERDICT_ASK_WITHOUT_CANDIDATE = "ask_without_candidate"
VERDICT_AUTO_DELETE = "auto_delete"

VERDICTS = frozenset(
    {
        VERDICT_AUTO_CORRECT,
        VERDICT_ASK_WITH_CANDIDATE,
        VERDICT_ASK_WITHOUT_CANDIDATE,
        VERDICT_AUTO_DELETE,
    }
)

FACT_LEXICAL_FLUENCY = "lexical_fluency"
FACT_FILLER_GARBLE = "filler_garble"
FACT_PROPER_NOUN = "proper_noun"
FACT_NUMERIC = "numeric"
FACT_DATETIME = "datetime"
FACT_DECISION = "decision"
FACT_UNCERTAIN = "uncertain"

FACT_CLASSES = frozenset(
    {
        FACT_LEXICAL_FLUENCY,
        FACT_FILLER_GARBLE,
        FACT_PROPER_NOUN,
        FACT_NUMERIC,
        FACT_DATETIME,
        FACT_DECISION,
        FACT_UNCERTAIN,
    }
)

FACT_SENSITIVE = frozenset(
    {
        FACT_PROPER_NOUN,
        FACT_NUMERIC,
        FACT_DATETIME,
        FACT_DECISION,
        FACT_UNCERTAIN,
    }
)

AUTO_OK_FACT_CLASSES = frozenset({FACT_LEXICAL_FLUENCY})
AUTO_DELETE_OK_FACT_CLASSES = frozenset({FACT_FILLER_GARBLE})

EDITOR_SOURCE = "contextual_editor"
EDITOR_TYPE = "contextual_editor"

INPUT_MECHANICAL = "merged_transcript_mechanical.txt"


def new_proposal_id() -> str:
    return str(uuid.uuid4())


def normalize_verdict(raw: object) -> str:
    v = str(raw or "").strip().lower()
    aliases = {
        "1": VERDICT_AUTO_CORRECT,
        "2": VERDICT_ASK_WITH_CANDIDATE,
        "3": VERDICT_ASK_WITHOUT_CANDIDATE,
        "4": VERDICT_AUTO_DELETE,
        "correct": VERDICT_AUTO_CORRECT,
        "ask": VERDICT_ASK_WITH_CANDIDATE,
        "delete": VERDICT_AUTO_DELETE,
    }
    if v in aliases:
        return aliases[v]
    if v in VERDICTS:
        return v
    return VERDICT_ASK_WITHOUT_CANDIDATE


def normalize_fact_class(raw: object) -> str:
    fc = str(raw or "").strip().lower()
    if fc in FACT_CLASSES:
        return fc
    return FACT_UNCERTAIN


def question_kind_for_verdict(verdict: str) -> str:
    if verdict == VERDICT_ASK_WITH_CANDIDATE:
        return "with_candidate"
    if verdict == VERDICT_ASK_WITHOUT_CANDIDATE:
        return "without_candidate"
    return ""


def enforce_fact_routing(proposal: dict[str, Any]) -> dict[str, Any]:
    """Layer 1: downgrade auto verdicts on fact-sensitive classes."""
    fc = normalize_fact_class(proposal.get("fact_class"))
    verdict = normalize_verdict(proposal.get("verdict"))
    proposal["fact_class"] = fc
    proposal["verdict"] = verdict

    downgraded = False
    if fc in FACT_SENSITIVE and verdict in (VERDICT_AUTO_CORRECT, VERDICT_AUTO_DELETE):
        downgraded = True
    elif verdict == VERDICT_AUTO_CORRECT and fc not in AUTO_OK_FACT_CLASSES:
        downgraded = True
    elif verdict == VERDICT_AUTO_DELETE and fc not in AUTO_DELETE_OK_FACT_CLASSES:
        downgraded = True

    if downgraded:
        proposal["original_verdict"] = verdict
        hypothesis = str(proposal.get("hypothesis") or "").strip()
        proposal["verdict"] = (
            VERDICT_ASK_WITH_CANDIDATE if hypothesis else VERDICT_ASK_WITHOUT_CANDIDATE
        )
        proposal["question_kind"] = question_kind_for_verdict(proposal["verdict"])
        if proposal.get("fact_class_source") != "code_override":
            proposal["routing_override"] = "fact_class_guard"
    else:
        proposal["question_kind"] = question_kind_for_verdict(verdict)

    return proposal


def to_legacy_anomaly(proposal: dict[str, Any]) -> dict[str, Any]:
    """Dual-read: coherence_review anomaly shape."""
    verdict = normalize_verdict(proposal.get("verdict"))
    return {
        "anomaly_id": proposal.get("proposal_id") or proposal.get("anomaly_id"),
        "anomaly_word": proposal.get("anomaly_word") or "",
        "span_text": proposal.get("span_before") or "",
        "span_corrected": proposal.get("span_after") or "",
        "estimated_correction": proposal.get("hypothesis") or "",
        "confidence": "high" if verdict == VERDICT_AUTO_CORRECT else "medium",
        "auto_fixable": verdict == VERDICT_AUTO_CORRECT,
        "reason": proposal.get("reason") or "",
        "context": proposal.get("evidence") or "",
        "span_start": proposal.get("span_start", -1),
        "span_end": proposal.get("span_end", -1),
        "anomaly_type": proposal.get("anomaly_type") or "B",
        "context_position_in_transcript": proposal.get("span_start", -1),
    }


def to_unknown_point(proposal: dict[str, Any]) -> dict[str, Any]:
    """Dual-read: unknown_points.json entry for LINE queue (②③ only)."""
    verdict = normalize_verdict(proposal.get("verdict"))
    if verdict not in (VERDICT_ASK_WITH_CANDIDATE, VERDICT_ASK_WITHOUT_CANDIDATE):
        raise ValueError(f"not a question verdict: {verdict}")
    span_before = str(proposal.get("span_before") or "").strip()
    return {
        "type": EDITOR_TYPE,
        "source": EDITOR_SOURCE,
        "status": "open",
        "verdict": verdict,
        "question_kind": proposal.get("question_kind")
        or question_kind_for_verdict(verdict),
        "text": str(proposal.get("evidence") or span_before)[:220],
        "context": span_before[:220],
        "evidence": str(proposal.get("evidence") or "")[:200],
        "hypothesis": str(proposal.get("hypothesis") or "").strip(),
        "reason": str(proposal.get("reason") or ""),
        "importance": str(proposal.get("importance") or ""),
        "anomaly_id": proposal.get("proposal_id"),
        "anomaly_word": proposal.get("anomaly_word") or "",
        "span_text": span_before,
        "span_corrected": str(proposal.get("span_after") or "").strip(),
        "span_start": proposal.get("span_start", -1),
        "fact_class": proposal.get("fact_class"),
        "context_position_in_transcript": proposal.get("span_start", -1),
    }
