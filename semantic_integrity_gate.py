"""Layer 3b: semantic integrity verification via LLM reading (Phase 10.2.2).

Complements fact_integrity_gate (layer 3): detects meaningful speech loss on delete
and unintended meaning change on correct.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import anthropic

from edit_proposal_schema import VERDICT_AUTO_CORRECT, VERDICT_AUTO_DELETE
from fact_integrity_gate import verify_fact_integrity

DEFAULT_SEMANTIC_GATE_MODEL = "claude-sonnet-4-6"
SEMANTIC_GATE_MODEL = os.environ.get(
    "SEMANTIC_INTEGRITY_GATE_MODEL", DEFAULT_SEMANTIC_GATE_MODEL
)
DEFAULT_WINDOW_CHARS = 450
SEMANTIC_GATE_BATCH_SIZE = max(
    1, int(os.environ.get("SEMANTIC_INTEGRITY_GATE_BATCH_SIZE", "1"))
)


def is_semantic_gate_retry_missing_enabled() -> bool:
    """Retry LLM once for omitted proposal_ids (default off; enable after measuring leak rate)."""
    raw = os.environ.get("SEMANTIC_GATE_RETRY_MISSING", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def count_semantic_issue_types(checks: list[dict[str, Any]]) -> dict[str, int]:
    """Count unverified semantic issues from proposal semantic_check dicts."""
    counts = {"missing_from_llm": 0, "no_llm_response": 0}
    for entry in checks:
        if not isinstance(entry, dict):
            continue
        issue = str(entry.get("issue") or "")
        if issue in counts:
            counts[issue] += 1
    return counts


@dataclass
class SemanticCheckResult:
    ok: bool
    issue: str = ""
    reason: str = ""
    proposal_id: str = ""
    source: str = "llm"  # llm | structural | skipped


@dataclass
class SemanticGateBatchResult:
    ok: bool
    checks: list[SemanticCheckResult] = field(default_factory=list)
    would_revert_ids: list[str] = field(default_factory=list)


def is_semantic_integrity_gate_enabled() -> bool:
    raw = os.environ.get("SEMANTIC_INTEGRITY_GATE_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def removed_beyond_garble(span_before: str, garble_fragment: str) -> str:
    """Text in span_before that is outside garble_fragment (editor overflow)."""
    span = str(span_before or "").strip()
    fragment = str(garble_fragment or "").strip()
    if not span or not fragment or span == fragment:
        return ""
    if fragment in span:
        idx = span.index(fragment)
        prefix = span[:idx].strip()
        suffix = span[idx + len(fragment) :].strip()
        return " ".join(x for x in (prefix, suffix) if x).strip()
    return span


def structural_delete_overreach(proposal: dict[str, Any]) -> SemanticCheckResult | None:
    """Fail when apply span is wider than editor's garble_fragment (no word lists)."""
    if str(proposal.get("verdict") or "") != VERDICT_AUTO_DELETE:
        return None
    span = str(proposal.get("span_before") or "").strip()
    fragment = str(proposal.get("garble_fragment") or "").strip()
    if not fragment:
        return None
    if span == fragment:
        return None
    overflow = removed_beyond_garble(span, fragment)
    if not overflow:
        return None
    pid = str(proposal.get("proposal_id") or "")
    return SemanticCheckResult(
        ok=False,
        issue="meaning_loss",
        reason=(
            f"span_before が garble_fragment より広い。"
            f"削除される余分な部分: {overflow[:120]}"
        ),
        proposal_id=pid,
        source="structural",
    )


def sync_delete_span_to_garble_fragment(
    proposal: dict[str, Any],
    text: str,
) -> bool:
    """Apply-time: shrink span_before to garble_fragment when it lies inside span."""
    if str(proposal.get("verdict") or "") != VERDICT_AUTO_DELETE:
        return False
    span = str(proposal.get("span_before") or "").strip()
    fragment = str(proposal.get("garble_fragment") or "").strip()
    if not fragment or span == fragment:
        return False
    start = int(proposal.get("span_start") or -1)
    if start < 0 or not span:
        return False
    if text[start : start + len(span)] != span:
        return False
    frag_in_span = span.find(fragment)
    if frag_in_span < 0:
        return False
    new_start = start + frag_in_span
    proposal["span_before_original"] = span
    proposal["span_start_original"] = start
    proposal["span_before"] = fragment
    proposal["span_start"] = new_start
    proposal["span_end"] = new_start + len(fragment)
    proposal["garble_span_synced"] = True
    proposal["garble_span_audit"] = proposal.get("garble_span_audit") or {}
    if isinstance(proposal["garble_span_audit"], dict):
        proposal["garble_span_audit"]["garble_fragment_match"] = True
        proposal["garble_span_audit"]["span_overflow_flags"] = []
    return True


def extract_edit_context_pair(
    original: str,
    trial: str,
    span_start: int,
    span_end: int,
    *,
    window_chars: int = DEFAULT_WINDOW_CHARS,
) -> tuple[str, str]:
    """Aligned before/after windows around an edit (delete or replace)."""
    win_start = max(0, span_start - window_chars)
    win_end = min(len(original), span_end + window_chars)
    before_ctx = original[win_start:win_end]
    delta = len(trial) - len(original)
    trial_win_end = min(len(trial), max(win_start, win_end + delta))
    after_ctx = trial[win_start:trial_win_end]
    return before_ctx, after_ctx


def build_trial_text_for_proposal(text: str, proposal: dict[str, Any]) -> str | None:
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


def _parse_semantic_response(raw: str) -> dict[str, Any]:
    s = (raw or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    try:
        loaded = json.loads(s)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        loaded = json.loads(s[start : end + 1])
        if isinstance(loaded, dict):
            return loaded
    raise RuntimeError(f"semantic_gate JSON parse failed: head={s[:200]!r}")


def _build_semantic_system_prompt() -> str:
    return (
        "あなたは議事録（逐語録）の編集監査者です。"
        "1件ずつ独立に判定してください。他提案と混同しないこと。"
        "\n\n【最重要 — auto_delete】"
        "\n1. まず `text_removed_by_apply`（実際に消える文字列）を**全文読む**。"
        " before/after 窓の差分だけに頼らない。"
        "\n2. `text_removed_by_apply` のうち `intended_garble_only` 以外の部分"
        "（`removed_beyond_garble`）に、説明・論点・業務内容・理由・手順として"
        "**価値のある発話**が含まれるなら **必ず ok=false**。"
        "\n3. 同じ話題が窓の別箇所に似た語句が残っていても、"
        "削除部分そのものが説明・論点なら ok=false（重複だから消してよい、と判断しない）。"
        "\n4. garble・言い淀み・言い直し残骸**だけ**が消える場合のみ ok=true。"
        "\n5. `editor_preserve_rationale` は編集者が「前後を残す理由」。"
        " removed_beyond_garble が説明価値ありなら preserve_rationale と整合し ok=false。"
        "\n\n【auto_correct】"
        "\n- 判定の中心は「置換後の語が、提示された前後文脈（窓）で一意に確定できるか」。"
        "\n- 会議の論旨・プロダクト名・固有名詞としての反復・タイトルと整合し、"
        "正しい語が一つに決まる置換は ok=true（同音でなくてもよい）。"
        "\n- 正しい語が一意に決まらない・複数候補がありうる内容語の置換は ok=false (meaning_change)。"
        " 文脈上不自然で読みが割れる置換（例: 映画→絵姿のように定訳が定まらないもの）もここ。"
        "\n- 同音異義・明らかな誤変換修正で論旨が保たれる場合は ok=true。"
        "\n- 置換で会議の論点・事実・説明内容が変わるなら ok=false。"
        "\n- 「同音だから」だけでなく、文脈から正しい語が一意かどうかで判断すること。"
        "\n\n【出力】JSON のみ:"
        '\n{"checks":[{"proposal_id":"...","ok":true|false,'
        '"issue":"none|meaning_loss|meaning_change","reason":"50字以内"}]}'
    )


def _build_audit_payload(
    *,
    before_text: str,
    after_text: str,
    proposal: dict[str, Any],
    window_chars: int,
) -> dict[str, Any]:
    start = int(proposal.get("span_start") or -1)
    span_before = str(proposal.get("span_before") or "")
    end = start + len(span_before)
    before_ctx, after_ctx = extract_edit_context_pair(
        before_text, after_text, start, end, window_chars=window_chars
    )
    fragment = str(proposal.get("garble_fragment") or "").strip()
    beyond = removed_beyond_garble(span_before, fragment)
    return {
        "proposal_id": str(proposal.get("proposal_id") or ""),
        "verdict": proposal.get("verdict"),
        "anomaly_word": proposal.get("anomaly_word") or "",
        "text_removed_by_apply": span_before,
        "intended_garble_only": fragment,
        "removed_beyond_garble": beyond,
        "span_matches_garble_fragment": span_before == fragment if fragment else None,
        "editor_preserve_rationale": str(proposal.get("preserve_rationale") or "")[:200],
        "span_after": str(proposal.get("span_after") or ""),
        "context_before_edit": before_ctx,
        "context_after_edit": after_ctx,
    }


def _unverified_semantic_check(proposal_id: str, issue: str) -> SemanticCheckResult:
    reasons = {
        "missing_from_llm": "LLM omitted proposal_id; held unverified",
        "no_llm_response": "LLM returned no checks; held unverified",
    }
    return SemanticCheckResult(
        ok=False,
        issue=issue,
        reason=reasons.get(issue, "held unverified"),
        proposal_id=proposal_id,
        source="llm",
    )


def _llm_semantic_checks_once(
    items: list[dict[str, Any]],
    *,
    api_key: str,
) -> list[SemanticCheckResult]:
    user_payload = json.dumps({"proposals_to_audit": items}, ensure_ascii=False, indent=2)
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=SEMANTIC_GATE_MODEL,
        max_tokens=2048,
        timeout=300,
        system=_build_semantic_system_prompt(),
        messages=[{"role": "user", "content": user_payload}],
    )
    raw_parts = [
        str(getattr(b, "text", "") or "")
        for b in getattr(resp, "content", []) or []
        if getattr(b, "type", "") == "text"
    ]
    parsed = _parse_semantic_response("\n".join(raw_parts))
    checks_raw = parsed.get("checks") or []
    results: list[SemanticCheckResult] = []
    for entry in checks_raw:
        if not isinstance(entry, dict):
            continue
        pid = str(entry.get("proposal_id") or "")
        results.append(
            SemanticCheckResult(
                ok=bool(entry.get("ok")),
                issue=str(entry.get("issue") or ("none" if entry.get("ok") else "unknown")),
                reason=str(entry.get("reason") or "")[:200],
                proposal_id=pid,
                source="llm",
            )
        )
    seen = {c.proposal_id for c in results}
    for item in items:
        pid = str(item.get("proposal_id") or "")
        if pid and pid not in seen:
            results.append(_unverified_semantic_check(pid, "missing_from_llm"))
    return results


def _llm_semantic_checks(
    items: list[dict[str, Any]],
    *,
    api_key: str,
) -> list[SemanticCheckResult]:
    if not items:
        return []
    results = _llm_semantic_checks_once(items, api_key=api_key)
    if not is_semantic_gate_retry_missing_enabled():
        return results
    missing_pids = [r.proposal_id for r in results if r.issue == "missing_from_llm"]
    if not missing_pids:
        return results
    missing_set = set(missing_pids)
    kept = [r for r in results if r.proposal_id not in missing_set]
    retry_items = [
        item for item in items if str(item.get("proposal_id") or "") in missing_set
    ]
    retry_results = _llm_semantic_checks_once(retry_items, api_key=api_key)
    return kept + retry_results


def verify_proposal_semantic_step(
    before_text: str,
    after_text: str,
    proposal: dict[str, Any],
    *,
    api_key: str | None = None,
    window_chars: int = DEFAULT_WINDOW_CHARS,
    skip_structural: bool = False,
) -> SemanticCheckResult:
    """Layer 3b for one apply step (before_text → after_text)."""
    pid = str(proposal.get("proposal_id") or "")
    if not skip_structural:
        structural = structural_delete_overreach(proposal)
        if structural is not None:
            return structural

    key = (api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set for semantic integrity gate.")

    item = _build_audit_payload(
        before_text=before_text,
        after_text=after_text,
        proposal=proposal,
        window_chars=window_chars,
    )
    checks = _llm_semantic_checks([item], api_key=key)
    if not checks:
        return _unverified_semantic_check(pid, "no_llm_response")
    return checks[0]


def verify_semantic_integrity_batch(
    *,
    text: str,
    proposals: list[dict[str, Any]],
    api_key: str | None = None,
    window_chars: int = DEFAULT_WINDOW_CHARS,
    skip_structural: bool = False,
) -> SemanticGateBatchResult:
    """Run layer 3b on proposals (simulation: each on full text)."""
    key = (api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set for semantic integrity gate.")

    auto_props = [
        p
        for p in proposals
        if str(p.get("verdict") or "")
        in (VERDICT_AUTO_CORRECT, VERDICT_AUTO_DELETE)
        and int(p.get("span_start") or -1) >= 0
    ]
    checks: list[SemanticCheckResult] = []
    would_revert: list[str] = []

    llm_queue: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for p in auto_props:
        pid = str(p.get("proposal_id") or "")
        if not skip_structural:
            structural = structural_delete_overreach(p)
            if structural is not None:
                checks.append(structural)
                would_revert.append(pid)
                continue

        trial = build_trial_text_for_proposal(text, p)
        if trial is None:
            continue
        item = _build_audit_payload(
            before_text=text,
            after_text=trial,
            proposal=p,
            window_chars=window_chars,
        )
        llm_queue.append((p, item))

    for i in range(0, len(llm_queue), SEMANTIC_GATE_BATCH_SIZE):
        chunk = llm_queue[i : i + SEMANTIC_GATE_BATCH_SIZE]
        items = [item for _, item in chunk]
        for result in _llm_semantic_checks(items, api_key=key):
            checks.append(result)
            if not result.ok and result.proposal_id:
                would_revert.append(result.proposal_id)

    return SemanticGateBatchResult(
        ok=len(would_revert) == 0,
        checks=checks,
        would_revert_ids=would_revert,
    )


def simulate_apply_with_dual_gates(
    text: str,
    proposals: list[dict[str, Any]],
    *,
    meeting_profile: dict[str, Any] | None = None,
    run_semantic: bool = False,
    api_key: str | None = None,
    sync_garble_span: bool = True,
    skip_structural: bool = False,
) -> dict[str, Any]:
    """Shadow simulation with fact + semantic gates."""
    from editor_apply import apply_proposals_with_gate

    work_props = [dict(p) for p in proposals]
    out_text, applied, reverted_fact, skipped, reverted_semantic = apply_proposals_with_gate(
        text,
        work_props,
        meeting_profile=meeting_profile,
        run_semantic=run_semantic,
        sync_garble_span=sync_garble_span,
        skip_structural_semantic=skip_structural,
        api_key=api_key,
    )
    fact_gate = verify_fact_integrity(text, out_text, meeting_profile=meeting_profile)

    semantic_summary: dict[str, Any] = {"enabled": run_semantic, "ok": None, "checks": []}
    if run_semantic:
        all_checks = [
            p.get("semantic_check")
            for p in work_props
            if isinstance(p.get("semantic_check"), dict)
        ]
        issue_counts = count_semantic_issue_types(all_checks)
        semantic_summary = {
            "enabled": True,
            "model": SEMANTIC_GATE_MODEL,
            "retry_missing_enabled": is_semantic_gate_retry_missing_enabled(),
            "ok": len(reverted_semantic) == 0,
            "would_revert_count": len(reverted_semantic),
            "would_revert_ids": [p.get("proposal_id") for p in reverted_semantic],
            "missing_from_llm_count": issue_counts["missing_from_llm"],
            "no_llm_response_count": issue_counts["no_llm_response"],
            "checks": all_checks,
            "sync_garble_span": sync_garble_span,
            "skip_structural": skip_structural,
        }

    return {
        "fact_gate": {"ok": fact_gate.ok, "violations": fact_gate.violations},
        "semantic_gate": semantic_summary,
        "applied_count": len(applied),
        "fact_reverted_count": len(reverted_fact),
        "semantic_reverted_count": len(reverted_semantic),
        "skipped_count": len(skipped),
    }
