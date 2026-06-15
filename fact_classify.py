"""Layer 2: code-side fact_class reclassification for edit proposals."""
from __future__ import annotations

import re
from typing import Any

from edit_proposal_schema import (
    FACT_DATETIME,
    FACT_DECISION,
    FACT_FILLER_GARBLE,
    FACT_LEXICAL_FLUENCY,
    FACT_NUMERIC,
    FACT_PROPER_NOUN,
    FACT_UNCERTAIN,
    normalize_fact_class,
)

_NUMERIC_RE = re.compile(
    r"(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?:万円|円|%|倍|人|名|件|時間|時|分|日|週|月|年)?"
    r"|\d+"
)
_DATETIME_RE = re.compile(
    r"(?:\d{1,2}時|\d{1,2}月|\d{1,2}日|来週|来月|今週|今月|年末|年始|年度|上期|下期)"
)
_DECISION_RE = re.compile(
    r"(決まった|合意|確定|承認|了承|〜で進める|で進める|方針として|決定)"
)
_FILLER_GARBLE_HINTS = re.compile(
    r"(ちょっと|えーと|あのー|うーん|まあ|なんか|っていう|かな$|だからな$)"
)


def _collect_participant_names(profile: dict[str, Any] | None) -> list[str]:
    if not profile:
        return []
    names: list[str] = []
    for key in ("participants", "attendees", "customer_names"):
        for item in profile.get(key) or []:
            s = str(item).strip()
            if s and s not in names:
                names.append(s)
    return names


def _collect_knowledge_terms(profile: dict[str, Any] | None) -> list[str]:
    terms: list[str] = []
    if not profile:
        return terms
    for memo in profile.get("relevant_knowledge") or []:
        for token in re.split(r"[\s、,・/]+", str(memo)):
            t = token.strip()
            if len(t) >= 2:
                terms.append(t)
    return terms


def _text_mentions_any(text: str, terms: list[str]) -> bool:
    for term in terms:
        if term and term in text:
            return True
    return False


def classify_fact_class(
    *,
    span_before: str,
    span_after: str = "",
    hypothesis: str = "",
    llm_fact_class: str = "",
    meeting_profile: dict[str, Any] | None = None,
    extra_place_names: list[str] | None = None,
) -> tuple[str, str]:
    """Return (fact_class, source) where source is llm or code_override."""
    llm = normalize_fact_class(llm_fact_class)
    combined = f"{span_before} {span_after} {hypothesis}"

    if _NUMERIC_RE.search(combined):
        return FACT_NUMERIC, "code_override"
    if _DATETIME_RE.search(combined):
        return FACT_DATETIME, "code_override"

    participants = _collect_participant_names(meeting_profile)
    knowledge = _collect_knowledge_terms(meeting_profile)
    place_names = list(extra_place_names or []) + knowledge
    if _text_mentions_any(combined, participants + place_names):
        return FACT_PROPER_NOUN, "code_override"

    if _DECISION_RE.search(combined) and (
        _NUMERIC_RE.search(combined) or _DATETIME_RE.search(combined)
    ):
        return FACT_DECISION, "code_override"

    if llm in {FACT_PROPER_NOUN, FACT_NUMERIC, FACT_DATETIME, FACT_DECISION}:
        return llm, "llm"

    if llm == FACT_FILLER_GARBLE or _FILLER_GARBLE_HINTS.search(span_before):
        if llm == FACT_FILLER_GARBLE:
            return FACT_FILLER_GARBLE, "llm"
        if not _NUMERIC_RE.search(span_before):
            return FACT_FILLER_GARBLE, "code_override"

    if llm == FACT_LEXICAL_FLUENCY:
        return FACT_LEXICAL_FLUENCY, "llm"

    if llm == FACT_UNCERTAIN:
        return FACT_UNCERTAIN, "llm"

    return llm or FACT_UNCERTAIN, "llm" if llm else "code_override"


def reclassify_proposal(
    proposal: dict[str, Any],
    *,
    meeting_profile: dict[str, Any] | None = None,
    extra_place_names: list[str] | None = None,
) -> dict[str, Any]:
    span_before = str(proposal.get("span_before") or "")
    span_after = str(proposal.get("span_after") or "")
    hypothesis = str(proposal.get("hypothesis") or "")
    fc, source = classify_fact_class(
        span_before=span_before,
        span_after=span_after,
        hypothesis=hypothesis,
        llm_fact_class=str(proposal.get("fact_class") or ""),
        meeting_profile=meeting_profile,
        extra_place_names=extra_place_names,
    )
    prev = normalize_fact_class(proposal.get("fact_class"))
    proposal["fact_class"] = fc
    if source == "code_override" and fc != prev:
        proposal["fact_class_source"] = "code_override"
    elif not proposal.get("fact_class_source"):
        proposal["fact_class_source"] = source
    return proposal
