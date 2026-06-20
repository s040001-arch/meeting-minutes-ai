"""Edit proposal schema (Phase 10 contextual editor).

Dual-read with coherence anomalies and unknown_points.
"""
from __future__ import annotations

import difflib
import re
import uuid
from typing import Any

from recognition_batch import find_standalone_word

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


def _collect_knowledge_place_names(profile: dict[str, Any] | None) -> list[str]:
    terms: list[str] = []
    if not profile:
        return terms
    for memo in profile.get("relevant_knowledge") or []:
        for token in str(memo).replace("、", " ").replace(",", " ").split():
            t = token.strip()
            if len(t) >= 2:
                terms.append(t)
    return terms


def protected_tokens_in_span(
    span_before: str,
    *,
    meeting_profile: dict[str, Any] | None = None,
    extra_place_names: list[str] | None = None,
) -> list[str]:
    """Proper nouns / participant names appearing in span_before."""
    tokens: list[str] = []
    seen: set[str] = set()
    candidates = (
        _collect_participant_names(meeting_profile)
        + list(extra_place_names or [])
        + _collect_knowledge_place_names(meeting_profile)
    )
    # Longest first to avoid partial double-counting
    for name in sorted(candidates, key=len, reverse=True):
        if name and name in span_before and name not in seen:
            tokens.append(name)
            seen.add(name)
    return tokens


def proper_noun_tokens_preserved(span_before: str, span_after: str, tokens: list[str]) -> bool:
    for token in tokens:
        if span_before.count(token) != span_after.count(token):
            return False
        if token not in span_after:
            return False
    return True


def enforce_proper_noun_immutability(
    proposal: dict[str, Any],
    *,
    meeting_profile: dict[str, Any] | None = None,
    extra_place_names: list[str] | None = None,
) -> dict[str, Any]:
    """Layer 1b: auto_correct must not alter protected tokens within the span."""
    if normalize_verdict(proposal.get("verdict")) != VERDICT_AUTO_CORRECT:
        return proposal
    span_before = str(proposal.get("span_before") or "")
    span_after = str(proposal.get("span_after") or "")
    tokens = protected_tokens_in_span(
        span_before,
        meeting_profile=meeting_profile,
        extra_place_names=extra_place_names,
    )
    if not tokens:
        return proposal
    if proper_noun_tokens_preserved(span_before, span_after, tokens):
        return proposal
    proposal["original_verdict"] = proposal.get("original_verdict") or VERDICT_AUTO_CORRECT
    hypothesis = str(proposal.get("hypothesis") or "").strip()
    proposal["verdict"] = (
        VERDICT_ASK_WITH_CANDIDATE if hypothesis else VERDICT_ASK_WITHOUT_CANDIDATE
    )
    proposal["question_kind"] = question_kind_for_verdict(proposal["verdict"])
    proposal["routing_override"] = "proper_noun_immutability_guard"
    return proposal


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


def _evidence_hint_pos(text: str, evidence: str) -> int:
    ev = str(evidence or "").strip()
    if not ev:
        return -1
    if ev in text:
        return text.find(ev)
    for n in (100, 80, 50, 30):
        chunk = ev[:n]
        if chunk and chunk in text:
            return text.find(chunk)
    return -1


def _find_substring_positions(text: str, needle: str) -> list[int]:
    positions: list[int] = []
    if not needle:
        return positions
    start = 0
    while start <= len(text):
        idx = text.find(needle, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + 1
    return positions


def _pick_near(positions: list[int], hint: int) -> int:
    if not positions:
        return -1
    if len(positions) == 1:
        return positions[0]
    if hint >= 0:
        return min(positions, key=lambda i: abs(i - hint))
    return positions[0]


def realign_span_after(
    span_before_llm: str,
    span_after_llm: str,
    span_before_actual: str,
) -> str:
    """Map an LLM span_after onto a corrected span_before (same edit intent)."""
    sb = str(span_before_llm or "")
    sa = str(span_after_llm or "")
    actual = str(span_before_actual or "")
    if not sa or sb == sa:
        return actual
    if sb == actual:
        return sa

    p = 0
    while p < min(len(sb), len(sa)) and sb[p] == sa[p]:
        p += 1
    s = 0
    while (
        s < min(len(sb) - p, len(sa) - p)
        and sb[len(sb) - 1 - s] == sa[len(sa) - 1 - s]
    ):
        s += 1
    old_mid = sb[p : len(sb) - s if s else len(sb)]
    new_mid = sa[p : len(sa) - s if s else len(sa)]
    if not old_mid:
        return actual
    mid_pos = actual.find(old_mid)
    if mid_pos < 0:
        return sa
    return actual[:mid_pos] + new_mid + actual[mid_pos + len(old_mid) :]


def _anchor_span_from_text(
    text: str,
    span_before_llm: str,
    anomaly_word: str,
    hint: int,
) -> tuple[int, str]:
    sb = str(span_before_llm or "").strip()
    if not sb:
        return -1, ""

    positions = _find_substring_positions(text, sb)
    if positions:
        pos = _pick_near(positions, hint)
        return pos, sb

    word = str(anomaly_word or "").strip()
    if not word:
        return -1, sb

    word_pos = find_standalone_word(text, word, hint_pos=hint)
    if word_pos < 0:
        word_pos = _pick_near(_find_substring_positions(text, word), hint)
    if word_pos < 0:
        return -1, sb

    aw_off = sb.find(word)
    if aw_off < 0:
        return word_pos, text[word_pos : word_pos + len(word)]

    start = word_pos - aw_off
    end = start + len(sb)
    if start >= 0 and end <= len(text) and text[start:end] == sb:
        return start, sb

    suffix = sb[aw_off + len(word) :]
    left = word_pos
    right = word_pos + len(word)
    if suffix and text[right : right + len(suffix)] == suffix:
        right += len(suffix)
    prefix = sb[:aw_off]
    for i in range(len(prefix) - 1, -1, -1):
        if left > 0 and text[left - 1] == prefix[i]:
            left -= 1
        else:
            break
    actual = text[left:right]
    if word in actual:
        return left, actual
    return word_pos, text[word_pos:right]


# Clause connectives that link a span to what follows it. When a ④ span_before
# leads with one of these before reaching the actual garble core (e.g. the LLM
# bundled a redo-remnant's connective context into the deletion span), the
# connective itself still does grammatical work for the surviving text and
# must not be deleted along with the remnant. Deliberately excludes bare
# fillers (えっと/あの/まあ/なんか) — those carry no link to following clauses
# and remain correctly auto-deletable as a whole.
_LEADING_CLAUSE_CONNECTOR_RE = re.compile(
    "^(?:"
    "だからそれは|だから|それでは|それで|それは|でも|だけど|けど(?:も)?|"
    "なので|つまり|ようするに|要は|そうしたら|そしたら|そうすると|"
    "じゃあ|では|そして|それから|まず|次に|結局|ところで|さて|"
    "というか|というのは|って"
    ")+"
)


def _strip_leading_clause_connector(span: str, anomaly_word: str) -> str | None:
    """Narrow a ④ span to drop a leading connector that precedes the garble core.

    General fix for the "接続詞・文頭語+言い直し残骸" over-deletion shape: detected
    structurally (connector prefix immediately followed by anomaly_word), not by
    hardcoding any specific transcript's wording.
    """
    span = str(span or "")
    aw = str(anomaly_word or "").strip()
    if not span or not aw or aw not in span:
        return None
    idx = span.find(aw)
    if idx <= 0:
        return None
    prefix = span[:idx]
    m = _LEADING_CLAUSE_CONNECTOR_RE.match(prefix)
    if not m or m.end() != len(prefix):
        return None
    narrowed = span[idx:]
    return narrowed if narrowed.strip() else None


def _minimal_garble_from_span(span: str, garble_fragment: str, anomaly_word: str) -> tuple[str, str]:
    span = str(span or "").strip()
    fragment = str(garble_fragment or "").strip()
    aw = str(anomaly_word or "").strip()

    narrowed = _strip_leading_clause_connector(fragment or span, aw)
    if narrowed is not None:
        return narrowed, narrowed

    if fragment and span and fragment in span:
        return fragment, fragment

    if aw and aw in span:
        idx = span.find(aw)
        start = idx
        if idx > 0 and span[idx - 1] in "、。，. ":
            start = idx - 1
        elif idx > 0:
            start = idx - 1
        garble = span[start : idx + len(aw)]
        return garble, garble

    if fragment:
        return fragment, fragment
    return span, fragment


def _resolve_garble_in_text(
    text: str,
    garble_fragment: str,
    anomaly_word: str,
    hint: int,
) -> tuple[int, str]:
    fragment = str(garble_fragment or "").strip()
    aw = str(anomaly_word or "").strip()
    needle = fragment or aw
    if not needle:
        return -1, ""

    positions = _find_substring_positions(text, needle)
    if positions:
        pos = _pick_near(positions, hint)
        span = needle
        if fragment and pos > 0 and text[pos - 1] in "、。，. ":
            span = text[pos - 1 : pos + len(fragment)]
            pos -= 1
        return pos, span

    if aw and aw != needle:
        word_pos = find_standalone_word(text, aw, hint_pos=hint)
        if word_pos < 0:
            word_pos = _pick_near(_find_substring_positions(text, aw), hint)
        if word_pos >= 0:
            start = word_pos
            if word_pos > 0 and text[word_pos - 1] in "、。，. ":
                start = word_pos - 1
            return start, text[start : word_pos + len(aw)]
    return -1, needle


def align_proposal_spans_in_text(
    text: str,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Ensure span_before is an exact transcript substring; garble_fragment ⊆ span_before."""
    span_before = str(proposal.get("span_before") or "").strip()
    span_after = str(proposal.get("span_after") or "").strip()
    span_before_llm = span_before
    span_after_llm = span_after
    garble_fragment = str(proposal.get("garble_fragment") or "").strip()
    anomaly_word = str(proposal.get("anomaly_word") or "").strip()
    evidence = str(proposal.get("evidence") or "").strip()
    verdict = normalize_verdict(proposal.get("verdict"))
    hint = _evidence_hint_pos(text, evidence)

    alignment: dict[str, Any] = {"status": "ok", "actions": []}

    if verdict == VERDICT_AUTO_DELETE:
        if garble_fragment and garble_fragment not in span_before:
            gpos, gspan = _resolve_garble_in_text(
                text, garble_fragment, anomaly_word, hint
            )
            if gpos >= 0 and text[gpos : gpos + len(gspan)] == gspan:
                span_before = gspan
                garble_fragment = gspan
                alignment["actions"].append("garble_relocated_to_text")
            elif garble_fragment and span_before:
                span_before, garble_fragment = _minimal_garble_from_span(
                    span_before, garble_fragment, anomaly_word
                )
                alignment["actions"].append("garble_shrunk_to_anomaly_in_span")
        elif garble_fragment and garble_fragment in span_before and span_before != garble_fragment:
            span_before = garble_fragment
            alignment["actions"].append("span_shrunk_to_garble_fragment")

    pos, actual_before = _anchor_span_from_text(
        text, span_before, anomaly_word, hint
    )
    if pos >= 0 and actual_before and text[pos : pos + len(actual_before)] == actual_before:
        if actual_before != span_before:
            alignment["actions"].append("span_before_realigned_to_text")
        span_before = actual_before
        if verdict == VERDICT_AUTO_CORRECT and span_after_llm:
            span_after = realign_span_after(span_before_llm, span_after_llm, span_before)
            if span_after != span_after_llm:
                alignment["actions"].append("span_after_realigned")
    else:
        proposal["span_start"] = -1
        proposal["span_end"] = -1
        proposal["span_alignment"] = {
            "status": "unresolvable",
            "reason": "span_before not found as contiguous substring in transcript",
            "span_before_llm": span_before_llm,
        }
        proposal["span_before"] = span_before
        proposal["span_after"] = span_after
        proposal["garble_fragment"] = garble_fragment
        return proposal

    if verdict == VERDICT_AUTO_DELETE:
        if _strip_leading_clause_connector(garble_fragment or span_before, anomaly_word):
            alignment["actions"].append("leading_connector_stripped")
        span_before, garble_fragment = _minimal_garble_from_span(
            span_before, garble_fragment, anomaly_word
        )
        if garble_fragment and garble_fragment in span_before and span_before != garble_fragment:
            span_before = garble_fragment
            alignment["actions"].append("span_minimized_to_garble")
        pos = text.find(span_before, max(0, pos - 20))
        if pos < 0:
            pos = text.find(span_before)

    if pos < 0 or not span_before or text[pos : pos + len(span_before)] != span_before:
        proposal["span_start"] = -1
        proposal["span_end"] = -1
        proposal["span_alignment"] = {
            "status": "unresolvable",
            "reason": "aligned span still not verifiable in transcript",
            "span_before": span_before,
        }
        proposal["span_before"] = span_before
        proposal["span_after"] = span_after
        proposal["garble_fragment"] = garble_fragment
        return proposal

    proposal["span_before"] = span_before
    proposal["span_after"] = span_after
    proposal["garble_fragment"] = garble_fragment
    proposal["span_start"] = pos
    proposal["span_end"] = pos + len(span_before)
    proposal["context_position_in_transcript"] = pos
    if alignment["actions"] or span_before_llm != span_before:
        alignment["span_before_llm"] = span_before_llm
        if span_after_llm != span_after:
            alignment["span_after_llm"] = span_after_llm
    proposal["span_alignment"] = alignment
    return proposal


def audit_garble_span(proposal: dict[str, Any]) -> dict[str, Any]:
    """Shadow audit: garble_fragment vs span_before alignment (no fixed width rules)."""
    verdict = normalize_verdict(proposal.get("verdict"))
    span = str(proposal.get("span_before") or "").strip()
    fragment = str(proposal.get("garble_fragment") or "").strip()
    anomaly = str(proposal.get("anomaly_word") or "").strip()
    evidence = str(proposal.get("evidence") or "").strip()

    flags: list[str] = []
    details: dict[str, str] = {}

    if verdict != VERDICT_AUTO_DELETE:
        return {
            "garble_fragment_match": None,
            "span_overflow_flags": flags,
            "span_overflow_details": details,
        }

    if not fragment:
        flags.append("missing_garble_fragment")
    elif span != fragment:
        flags.append("span_garble_fragment_mismatch")
        if fragment in span:
            idx = span.index(fragment)
            prefix = span[:idx].strip()
            suffix = span[idx + len(fragment) :].strip()
            if prefix:
                flags.append("span_prefix_beyond_garble_fragment")
                details["overflow_prefix"] = prefix
            if suffix:
                flags.append("span_suffix_beyond_garble_fragment")
                details["overflow_suffix"] = suffix
        elif span and fragment not in span:
            flags.append("garble_fragment_not_substring_of_span")

    if span and anomaly:
        pos = span.find(anomaly)
        if pos > 0:
            prefix_before_anomaly = span[:pos].strip()
            if prefix_before_anomaly and prefix_before_anomaly not in evidence:
                flags.append("span_extends_before_anomaly_beyond_evidence")
                details["prefix_before_anomaly"] = prefix_before_anomaly

    if span and evidence and span not in evidence:
        # span substantially wider than cited evidence (contextual flag, not char threshold)
        if evidence in span:
            extra = span.replace(evidence, "", 1).strip()
            if extra:
                flags.append("span_wider_than_evidence_citation")
                details["beyond_evidence"] = extra[:120]

    return {
        "garble_fragment_match": span == fragment if fragment else False,
        "span_overflow_flags": flags,
        "span_overflow_details": details,
    }


def summarize_garble_audits(proposals: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate garble span audits for shadow report."""
    delete_props = [
        p for p in proposals if normalize_verdict(p.get("verdict")) == VERDICT_AUTO_DELETE
    ]
    audits = [audit_garble_span(p) for p in delete_props]
    match_count = sum(1 for a in audits if a.get("garble_fragment_match") is True)
    flagged = []
    for p in delete_props:
        audit = audit_garble_span(p)
        if audit.get("span_overflow_flags") or audit.get("garble_fragment_match") is False:
            flagged.append(
                {
                    "proposal_id": p.get("proposal_id"),
                    "anomaly_word": p.get("anomaly_word"),
                    "span_before": p.get("span_before"),
                    "garble_fragment": p.get("garble_fragment"),
                    "preserve_rationale": p.get("preserve_rationale"),
                    **audit,
                }
            )
    return {
        "auto_delete_count": len(delete_props),
        "garble_fragment_match_count": match_count,
        "garble_fragment_match_rate": (
            round(match_count / len(delete_props), 3) if delete_props else None
        ),
        "span_overflow_flag_count": sum(
            1 for a in audits if a.get("span_overflow_flags")
        ),
        "flagged_proposals": flagged,
    }


_REASON_CORRECTION_PAIR_RE = re.compile(
    r"「([^」]+)」(?:は|→)\s*(?:「([^」]+)」|([^。、\s]+(?:の)?[^。、\s]*))"
)


def _find_span_fuzzy(haystack: str, needle: str) -> tuple[int, int] | None:
    """Return (start, end) in haystack matching needle, ignoring extra whitespace."""
    n = re.sub(r"\s+", "", str(needle or ""))
    if not n:
        return None
    ni = 0
    start = -1
    end = -1
    for i, ch in enumerate(str(haystack or "")):
        if ch.isspace():
            continue
        if ni >= len(n):
            break
        if ch != n[ni]:
            ni = 0
            start = -1
            continue
        if ni == 0:
            start = i
        ni += 1
        end = i + 1
    if ni == len(n) and start >= 0:
        return start, end
    return None


def extract_correction_pair_from_reason(
    reason: str,
    importance: str = "",
    span_before: str = "",
) -> tuple[str, str] | None:
    """Parse 「誤」は「正」 from reason/importance for empty-① repair."""
    for blob in (reason, importance):
        for m in _REASON_CORRECTION_PAIR_RE.finditer(str(blob or "")):
            wrong = str(m.group(1) or "").strip()
            right = str(m.group(2) or m.group(3) or "").strip()
            right = re.sub(r"(?:の)?(?:同音誤り|崩れ|誤変換).*$", "", right).strip()
            if not wrong or not right or wrong == right:
                continue
            if span_before:
                if wrong not in span_before and _find_span_fuzzy(span_before, wrong) is None:
                    continue
            return wrong, right
    return None


def extract_hypothesis_from_reason(reason: str, importance: str = "") -> str:
    """Best-effort candidate from reason like 「X」は「Y」等の崩れ."""
    for blob in (reason, importance):
        m = re.search(r"「[^」]+」は「([^」]+)」", str(blob or ""))
        if m:
            cand = str(m.group(1) or "").strip()
            if cand and len(cand) <= 15:
                return cand
    return ""


def repair_span_after_from_correction(
    span_before: str,
    wrong: str,
    right: str,
) -> str | None:
    """Build span_after by replacing wrong→right once inside span_before."""
    sb = str(span_before or "")
    if not sb or not wrong or not right or wrong == right:
        return None
    if wrong in sb:
        repaired = sb.replace(wrong, right, 1)
    else:
        loc = _find_span_fuzzy(sb, wrong)
        if not loc:
            return None
        start, end = loc
        repaired = sb[:start] + right + sb[end:]
    if not repaired.strip() or repaired == sb:
        return None
    return repaired


def is_clear_filler_garble_for_downgrade(span_before: str, proposal: dict[str, Any]) -> bool:
    """④ downgrade only when span is unambiguous filler/garble — not substantive speech."""
    from fact_classify import classify_fact_class, looks_like_garble_span

    span = str(span_before or "").strip()
    if not span or not looks_like_garble_span(span):
        return False
    # Clause-length speech with sentence break → ask, do not delete.
    if "。" in span and len(span) > 12:
        return False
    fc, _ = classify_fact_class(
        span_before=span,
        span_after="",
        llm_fact_class=str(proposal.get("fact_class") or ""),
        llm_verdict=VERDICT_AUTO_DELETE,
    )
    return fc == FACT_FILLER_GARBLE


def remediate_empty_auto_correct(proposal: dict[str, Any]) -> dict[str, Any]:
    """Reject empty ①: repair span_after, else ②③ (prefer ask) or ④ if clear garble only."""
    verdict = normalize_verdict(proposal.get("verdict"))
    if verdict != VERDICT_AUTO_CORRECT:
        return proposal
    span_before = str(proposal.get("span_before") or "").strip()
    span_after = str(proposal.get("span_after") or "").strip()
    if span_after:
        return proposal

    reason = str(proposal.get("reason") or "")
    importance = str(proposal.get("importance") or "")

    pair = extract_correction_pair_from_reason(reason, importance, span_before)
    if pair:
        wrong, right = pair
        repaired = repair_span_after_from_correction(span_before, wrong, right)
        if repaired:
            proposal["span_after"] = repaired
            proposal["empty_auto_correct_remediation"] = "span_after_repaired"
            return proposal

    if is_clear_filler_garble_for_downgrade(span_before, proposal):
        proposal["original_verdict"] = VERDICT_AUTO_CORRECT
        proposal["verdict"] = VERDICT_AUTO_DELETE
        proposal["span_after"] = ""
        if not str(proposal.get("garble_fragment") or "").strip():
            proposal["garble_fragment"] = span_before
        proposal["fact_class"] = FACT_FILLER_GARBLE
        proposal["empty_auto_correct_remediation"] = "downgrade_to_auto_delete"
        return proposal

    proposal["original_verdict"] = VERDICT_AUTO_CORRECT
    hypothesis = str(proposal.get("hypothesis") or "").strip()
    if not hypothesis:
        hypothesis = extract_hypothesis_from_reason(reason, importance)
        if hypothesis:
            proposal["hypothesis"] = hypothesis
    proposal["verdict"] = (
        VERDICT_ASK_WITH_CANDIDATE if hypothesis else VERDICT_ASK_WITHOUT_CANDIDATE
    )
    proposal["span_after"] = ""
    proposal["empty_auto_correct_remediation"] = (
        "downgrade_to_ask_with_candidate"
        if hypothesis
        else "downgrade_to_ask_without_candidate"
    )
    proposal["routing_override"] = (
        proposal.get("routing_override") or "empty_auto_correct_guard"
    )
    return proposal


def _paragraph_bounds(text: str, pos: int) -> tuple[int, int]:
    """Paragraph window around pos (。 and blank-line boundaries)."""
    if not text or pos < 0:
        return 0, len(text)
    start = text.rfind("。", 0, pos)
    start = 0 if start < 0 else start + 1
    nl = text.rfind("\n\n", 0, pos)
    if nl >= 0 and nl + 2 > start:
        start = nl + 2
    end = text.find("。", pos)
    end = len(text) if end < 0 else end + 1
    nl2 = text.find("\n\n", pos)
    if nl2 >= 0 and nl2 < end:
        end = nl2
    return start, end


def _correction_pair_from_proposal(proposal: dict[str, Any]) -> tuple[str, str] | None:
    span_before = str(proposal.get("span_before") or "")
    span_after = str(proposal.get("span_after") or "")
    reason = str(proposal.get("reason") or "")
    importance = str(proposal.get("importance") or "")
    pair = extract_correction_pair_from_reason(reason, importance, span_before)
    if pair:
        return pair
    if not span_before or not span_after or span_before == span_after:
        return None
    old_parts: list[str] = []
    new_parts: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, span_before, span_after
    ).get_opcodes():
        if tag == "equal":
            continue
        old_parts.append(span_before[i1:i2])
        new_parts.append(span_after[j1:j2])
    if not old_parts:
        return None
    wrong = "".join(old_parts).strip()
    right = "".join(new_parts).strip()
    if wrong and right and wrong != right:
        return wrong, right
    return None


def _homophone_plausible(wrong: str, right: str) -> bool:
    """Phonetic plausibility without a fixed vocabulary (character-level proxy)."""
    wrong = str(wrong or "").strip()
    right = str(right or "").strip()
    if not wrong or not right:
        return True
    if wrong == right:
        return True
    ratio = difflib.SequenceMatcher(None, wrong, right).ratio()
    if ratio >= 0.34:
        return True
    if len(wrong) == len(right) and len(wrong) <= 4:
        same = sum(1 for a, b in zip(wrong, right) if a == b)
        if same >= len(wrong) - 1:
            return True
    return False


def _reason_claims_homophone(reason: str, importance: str = "") -> bool:
    blob = f"{reason} {importance}"
    return "同音" in blob


def _span_edit_similarity(span_before: str, span_after: str) -> float:
    sb = str(span_before or "")
    sa = str(span_after or "")
    if not sb or not sa:
        return 0.0
    return difflib.SequenceMatcher(None, sb, sa).ratio()


def _span_after_repeated_earlier_in_paragraph(
    text: str,
    span_after: str,
    pos: int,
    *,
    lookback: int = 1200,
) -> bool:
    """True when the full corrected span already appeared in recent context before pos."""
    sa = str(span_after or "").strip()
    if len(sa) < 10 or pos < 0:
        return False
    earlier = text[max(0, pos - lookback) : pos]
    return sa in earlier


def _fluency_fix_bypass(
    reason: str,
    importance: str,
    *,
    span_sim: float,
    wrong: str,
    right: str,
) -> bool:
    """Allow ① when Opus cites homophone/崩れ and the edit is locally tight."""
    blob = f"{reason} {importance}"
    if "同音" in blob and span_sim >= 0.70:
        return True
    if "崩れ" in blob and len(wrong) <= 2 and span_sim >= 0.80:
        return True
    return False


def _peer_ask_in_same_paragraph(
    proposal: dict[str, Any],
    *,
    text: str,
    peer_proposals: list[dict[str, Any]],
) -> bool:
    pos = int(proposal.get("span_start") or -1)
    if pos < 0:
        return False
    p0, p1 = _paragraph_bounds(text, pos)
    for peer in peer_proposals:
        if peer is proposal:
            continue
        if normalize_verdict(peer.get("verdict")) not in (
            VERDICT_ASK_WITH_CANDIDATE,
            VERDICT_ASK_WITHOUT_CANDIDATE,
        ):
            continue
        qpos = int(peer.get("span_start") or -1)
        if qpos >= 0 and p0 <= qpos < p1:
            return True
    return False


def _downgrade_lexical_auto_to_ask(
    proposal: dict[str, Any],
    *,
    remediation: str,
) -> None:
    proposal["original_verdict"] = (
        proposal.get("original_verdict") or VERDICT_AUTO_CORRECT
    )
    hypothesis = str(proposal.get("hypothesis") or "").strip()
    proposal["verdict"] = (
        VERDICT_ASK_WITH_CANDIDATE if hypothesis else VERDICT_ASK_WITHOUT_CANDIDATE
    )
    proposal["question_kind"] = question_kind_for_verdict(proposal["verdict"])
    proposal["span_after"] = ""
    proposal["lexical_auto_correct_remediation"] = remediation
    proposal["routing_override"] = (
        proposal.get("routing_override") or "lexical_ambiguity_guard"
    )


def enforce_ambiguous_lexical_auto_correct(
    proposal: dict[str, Any],
    *,
    text: str | None = None,
    peer_proposals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Layer 1b: downgrade ① when content-word correction is not uniquely determined."""
    verdict = normalize_verdict(proposal.get("verdict"))
    fc = normalize_fact_class(proposal.get("fact_class"))
    if verdict != VERDICT_AUTO_CORRECT or fc != FACT_LEXICAL_FLUENCY:
        return proposal
    span_before = str(proposal.get("span_before") or "").strip()
    span_after = str(proposal.get("span_after") or "").strip()
    if not span_before or not span_after:
        return proposal

    pair = _correction_pair_from_proposal(proposal)
    if not pair:
        return proposal
    wrong, right = pair
    homophone_ok = _homophone_plausible(wrong, right)
    span_sim = _span_edit_similarity(span_before, span_after)

    remediation: str | None = None
    homophone_bypass = _fluency_fix_bypass(
        str(proposal.get("reason") or ""),
        str(proposal.get("importance") or ""),
        span_sim=span_sim,
        wrong=wrong,
        right=right,
    )
    if not homophone_ok and not homophone_bypass:
        remediation = "downgrade_non_homophone_content_word"
    elif text and _span_after_repeated_earlier_in_paragraph(
        text,
        span_after,
        int(proposal.get("span_start") or -1),
    ):
        remediation = "downgrade_repetition_inference"
    elif (
        text
        and peer_proposals
        and not homophone_ok
        and not homophone_bypass
        and _peer_ask_in_same_paragraph(
            proposal, text=text, peer_proposals=peer_proposals
        )
    ):
        remediation = "downgrade_paragraph_peer_ask_inconsistency"

    if remediation:
        _downgrade_lexical_auto_to_ask(proposal, remediation=remediation)
    return proposal


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
