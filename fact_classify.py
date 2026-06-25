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
    VERDICT_AUTO_DELETE,
    normalize_fact_class,
    normalize_verdict,
)

# Substantive business numbers — a digit run directly adjacent to a kanji counter
# suffix (structural Japanese counter-word pattern), not an enumerated unit list.
# A fixed whitelist (万円/人/件/...) misses garbled units entirely, e.g. STT
# rendering "1000社" as "1000車" — the wrong unit just isn't in the list, so the
# anomaly silently classifies as lexical_fluency and slips past the fact guard.
_SUBSTANTIVE_NUMERIC_RE = re.compile(
    r"(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?:%|[一-龥]{1,2})"
)
# Clear schedule/commitment: 「16時開催」「6月1日まで」— not 「16時にちょっという」崩れ片。
_SUBSTANTIVE_DATETIME_RE = re.compile(
    r"(?:\d{1,2}\s*時\s*(?:開催|開始|終了|から|まで|に)|"
    r"\d{1,2}\s*月\s*\d{1,2}\s*日|来週|来月|今週|今月|年末|年始|年度|上期|下期)"
)
_DECISION_RE = re.compile(
    r"(決まった|合意|確定|承認|了承|〜で進める|で進める|方針として|決定)"
)
_FILLER_GARBLE_HINTS = re.compile(
    r"(ちょっと|ちょっ|えーと|あのー|うーん|まあ|なんか|っていう|という[。、]?$|かな$|だからな$|"
    r"言い直し|えっと|あの、)"
)
_GARBLE_FRAGMENT_RE = re.compile(
    r"(こうかだから|だからな|っという|言い直|てるんですけども?$|ですけども?$)"
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


def looks_like_garble_span(span_before: str) -> bool:
    """True when span reads as filler / broken utterance, not a factual statement."""
    s = str(span_before or "").strip()
    if not s:
        return False
    if _FILLER_GARBLE_HINTS.search(s) or _GARBLE_FRAGMENT_RE.search(s):
        return True
    # Short fragments ending mid-phrase
    if len(s) <= 25 and s.endswith(("という", "ってい", "かな", "だからな", "ですけど")):
        return True
    return False


def is_substantive_numeric(text: str) -> bool:
    """Amounts / units — not a lone digit embedded in garble."""
    return bool(_SUBSTANTIVE_NUMERIC_RE.search(text))


def is_substantive_datetime(text: str) -> bool:
    """Scheduled times/dates as facts — not garble like 「16時にちょっという」."""
    if looks_like_garble_span(text):
        return False
    return bool(_SUBSTANTIVE_DATETIME_RE.search(text))


def classify_fact_class(
    *,
    span_before: str,
    span_after: str = "",
    hypothesis: str = "",
    llm_fact_class: str = "",
    llm_verdict: str = "",
    meeting_profile: dict[str, Any] | None = None,
    extra_place_names: list[str] | None = None,
) -> tuple[str, str]:
    """Return (fact_class, source) where source is llm or code_override."""
    llm = normalize_fact_class(llm_fact_class)
    verdict = normalize_verdict(llm_verdict) if llm_verdict else ""
    combined = f"{span_before} {span_after} {hypothesis}"
    garble = looks_like_garble_span(span_before)

    # Respect LLM filler_garble + auto_delete (④ path).
    if llm == FACT_FILLER_GARBLE and verdict == VERDICT_AUTO_DELETE:
        return FACT_FILLER_GARBLE, "llm"
    if garble and llm == FACT_FILLER_GARBLE:
        return FACT_FILLER_GARBLE, "llm"
    if garble and verdict == VERDICT_AUTO_DELETE:
        return FACT_FILLER_GARBLE, "code_override"

    # Garble-internal digits: do not promote to numeric/datetime.
    if not garble:
        if is_substantive_numeric(combined):
            return FACT_NUMERIC, "code_override"
        if is_substantive_datetime(combined):
            return FACT_DATETIME, "code_override"

    participants = _collect_participant_names(meeting_profile)
    knowledge = _collect_knowledge_terms(meeting_profile)
    place_names = list(extra_place_names or []) + knowledge
    if _text_mentions_any(combined, participants + place_names):
        return FACT_PROPER_NOUN, "code_override"

    if _DECISION_RE.search(combined) and (
        is_substantive_numeric(combined) or is_substantive_datetime(combined)
    ):
        return FACT_DECISION, "code_override"

    if llm in {FACT_PROPER_NOUN, FACT_NUMERIC, FACT_DATETIME, FACT_DECISION}:
        # Downgrade LLM numeric/datetime when span is actually garble.
        if garble and llm in {FACT_NUMERIC, FACT_DATETIME}:
            return FACT_FILLER_GARBLE, "code_override"
        return llm, "llm"

    if garble:
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
        llm_verdict=str(proposal.get("verdict") or ""),
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
