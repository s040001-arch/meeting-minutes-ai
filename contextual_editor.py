"""Step 4.25: Contextual transcript editor (Phase 10).

Full-text Opus pass that proposes contextual edits with 4-outcome verdict routing.
Shadow mode (default): proposals + report only; transcript unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Any

import anthropic

from anthropic_prompt_cache import OPUS_MODEL_ID, cached_system
from edit_proposal_schema import (
    EDITOR_SOURCE,
    INPUT_MECHANICAL,
    VERDICT_AUTO_CORRECT,
    VERDICT_AUTO_DELETE,
    VERDICT_ASK_WITH_CANDIDATE,
    VERDICT_ASK_WITHOUT_CANDIDATE,
    enforce_fact_routing,
    new_proposal_id,
    normalize_fact_class,
    normalize_verdict,
)
from fact_classify import reclassify_proposal
from fact_integrity_gate import simulate_apply_proposals, verify_fact_integrity
from meeting_profile import format_meeting_profile_for_prompt, load_meeting_profile
from pipeline_build import get_pipeline_build_info
from recognition_batch import find_standalone_word

CONTEXTUAL_EDITOR_MODEL = OPUS_MODEL_ID
CONTEXTUAL_EDITOR_MAX_TOKENS = 32000
CONTEXTUAL_EDITOR_TIMEOUT_SEC = 900
PIPELINE_EDITOR_VERSION = "20260610-phase10-shadow-v1"

EDIT_PROPOSALS_FILENAME = "edit_proposals.json"
EDITOR_SHADOW_REPORT_FILENAME = "editor_shadow_report.json"

FULL_TEXT_CHAR_CAP = 60_000


def is_contextual_editor_enabled() -> bool:
    raw = os.environ.get("CONTEXTUAL_EDITOR_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def resolve_editor_mode(cli_mode: str | None = None) -> str:
    if cli_mode:
        return cli_mode.strip().lower()
    return os.environ.get("CONTEXTUAL_EDITOR_MODE", "shadow").strip().lower() or "shadow"


def _load_anthropic_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    return key


def _build_system_prompt(meeting_profile: dict | None) -> str | list:
    profile_block = format_meeting_profile_for_prompt(meeting_profile or {})
    world_block = ""
    try:
        from world_knowledge_store import get_runtime_knowledge_block

        world_block = get_runtime_knowledge_block(
            meeting_profile=meeting_profile, purpose="coherence",
        )
    except Exception as e:  # noqa: BLE001
        print(f"contextual_editor_world_knowledge_fetch_failed={e!r}")

    static_prompt = (
        "あなたは議事録（逐語録）の全文編集者です。"
        "Google Pixel 音声認識を機械補正した日本語議事録を**全文一読**し、"
        "各問題箇所を文脈から判断して JSON 配列で提案してください。"
        "\n\n【4帰結（verdict）— 固定閾値ではなく文脈で都度判断】"
        "\n- auto_correct (①): 一般語・同音異義の誤りで文脈が明確。事実に触れない。"
        "\n- ask_with_candidate (②): 固有名詞・数値・決定等、または候補を1つ示せる疑い。"
        "\n- ask_without_candidate (③): 聞くべきだが候補を捏造できない（②と同様に重要）。"
        "\n- auto_delete (④): フィラー・言い淀み・意味のない崩れ片のみ（filler_garble）。"
        "\n\n【鉄の掟】"
        "\n- 固有名詞・数値・金額・日時・決定事項は確信が高くても auto_correct / auto_delete 禁止。"
        "必ず ask_with_candidate または ask_without_candidate。"
        "\n- 候補のない固有名詞は ask_without_candidate（候補を捏造しない）。"
        "\n- 数値閾値やスコアで省略しない。聞くべき箇所は②③に載せる。"
        "\n\n【出力スキーマ】JSON 配列のみ。各要素:"
        '\n{"span_before":"本文中の原文スパン（20-120字）",'
        '"span_after":"①のみ修正後スパン。②③④は空文字",'
        '"verdict":"auto_correct|ask_with_candidate|ask_without_candidate|auto_delete",'
        '"fact_class":"lexical_fluency|filler_garble|proper_noun|numeric|datetime|decision|uncertain",'
        '"hypothesis":"②のみ候補語（1-15字）。それ以外は空",'
        '"evidence":"前後30-100字の引用",'
        '"importance":"なぜこの帰結か（叙述・1文）",'
        '"reason":"判定根拠（50字以内）",'
        '"anomaly_word":"スパン中心語（質問表示用）"}'
        "\n\n問題が無ければ [] を返す。"
    )
    return cached_system(static_prompt, profile_block + world_block)


def _extract_text_from_anthropic(resp) -> str:
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(p for p in parts if p).strip()


def _parse_json_array(raw: str) -> list[dict]:
    s = (raw or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    if not s:
        return []
    try:
        loaded = json.loads(s)
        if isinstance(loaded, list):
            return [x for x in loaded if isinstance(x, dict)]
    except json.JSONDecodeError:
        pass
    start = s.find("[")
    end = s.rfind("]")
    if start >= 0 and end > start:
        loaded = json.loads(s[start : end + 1])
        if isinstance(loaded, list):
            return [x for x in loaded if isinstance(x, dict)]
    raise RuntimeError(f"contextual_editor JSON parse failed: head={s[:200]!r}")


def _call_opus_for_proposals(text: str, meeting_profile: dict | None) -> list[dict]:
    client = anthropic.Anthropic(api_key=_load_anthropic_api_key())
    system_prompt = _build_system_prompt(meeting_profile)
    payload_text = text if len(text) <= FULL_TEXT_CHAR_CAP else text[:FULL_TEXT_CHAR_CAP]
    resp = client.messages.create(
        model=CONTEXTUAL_EDITOR_MODEL,
        max_tokens=CONTEXTUAL_EDITOR_MAX_TOKENS,
        timeout=CONTEXTUAL_EDITOR_TIMEOUT_SEC,
        system=system_prompt,
        messages=[{"role": "user", "content": payload_text}],
    )
    raw = _extract_text_from_anthropic(resp)
    return _parse_json_array(raw)


def _resolve_span_position(text: str, span_before: str, anomaly_word: str) -> int:
    span = str(span_before or "").strip()
    if span and span in text:
        return text.find(span)
    word = str(anomaly_word or "").strip()
    if word:
        pos = find_standalone_word(text, word)
        if pos >= 0:
            return pos
    if span:
        short = span[: min(20, len(span))]
        if short:
            return text.find(short)
    return -1


def _enrich_proposal(
    raw: dict,
    idx: int,
    text: str,
    *,
    meeting_profile: dict | None,
) -> dict[str, Any]:
    span_before = str(raw.get("span_before") or "").strip()
    span_after = str(raw.get("span_after") or "").strip()
    anomaly_word = str(raw.get("anomaly_word") or "").strip()
    if not anomaly_word and span_before:
        anomaly_word = span_before[: min(15, len(span_before))]

    pos = _resolve_span_position(text, span_before, anomaly_word)
    if pos >= 0 and span_before and text[pos : pos + len(span_before)] != span_before:
        if len(span_before) > 40:
            span_before = text[pos : pos + min(len(span_before), 120)]

    proposal: dict[str, Any] = {
        "proposal_id": str(raw.get("proposal_id") or new_proposal_id()),
        "verdict": normalize_verdict(raw.get("verdict")),
        "span_before": span_before,
        "span_after": span_after,
        "fact_class": normalize_fact_class(raw.get("fact_class")),
        "fact_class_source": "llm",
        "hypothesis": str(raw.get("hypothesis") or "").strip(),
        "evidence": str(raw.get("evidence") or "").strip()[:200],
        "importance": str(raw.get("importance") or "").strip()[:200],
        "reason": str(raw.get("reason") or "").strip()[:120],
        "anomaly_word": anomaly_word,
        "span_start": pos,
        "span_end": pos + len(span_before) if pos >= 0 else -1,
        "context_position_in_transcript": pos,
        "applied": False,
    }

    reclassify_proposal(proposal, meeting_profile=meeting_profile)
    enforce_fact_routing(proposal)
    return proposal


def _build_shadow_report(
    *,
    job_id: str,
    mode: str,
    text: str,
    proposals: list[dict[str, Any]],
    meeting_profile: dict | None,
) -> dict[str, Any]:
    verdict_counts: dict[str, int] = {}
    fact_class_counts: dict[str, int] = {}
    guard_downgrades: list[dict[str, Any]] = []

    for p in proposals:
        v = str(p.get("verdict") or "")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
        fc = str(p.get("fact_class") or "")
        fact_class_counts[fc] = fact_class_counts.get(fc, 0) + 1
        if p.get("routing_override") == "fact_class_guard" or p.get("original_verdict"):
            guard_downgrades.append(
                {
                    "proposal_id": p.get("proposal_id"),
                    "anomaly_word": p.get("anomaly_word"),
                    "span_before": (p.get("span_before") or "")[:80],
                    "original_verdict": p.get("original_verdict"),
                    "verdict": p.get("verdict"),
                    "fact_class": p.get("fact_class"),
                    "hypothesis": p.get("hypothesis"),
                }
            )

    simulated = simulate_apply_proposals(text, proposals)
    gate = verify_fact_integrity(text, simulated, meeting_profile=meeting_profile)
    auto_count = sum(
        1
        for p in proposals
        if p.get("verdict") in (VERDICT_AUTO_CORRECT, VERDICT_AUTO_DELETE)
    )
    would_fail_rate = 0.0 if auto_count == 0 else (0.0 if gate.ok else 1.0)

    def _find_examples(needles: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for needle in needles:
            for p in proposals:
                blob = f"{p.get('span_before')} {p.get('evidence')} {p.get('anomaly_word')}"
                if needle in blob:
                    out.append(
                        {
                            "needle": needle,
                            "proposal_id": p.get("proposal_id"),
                            "verdict": p.get("verdict"),
                            "fact_class": p.get("fact_class"),
                            "hypothesis": p.get("hypothesis"),
                            "span_before": (p.get("span_before") or "")[:100],
                            "original_verdict": p.get("original_verdict"),
                            "routing_override": p.get("routing_override"),
                        }
                    )
                    break
        return out

    fact_needles = ["85万", "75万", "10万", "58万", "70万", "五反田", "横浜", "八戸"]
    fact_in_auto = [
        {
            "proposal_id": p.get("proposal_id"),
            "verdict": p.get("verdict"),
            "fact_class": p.get("fact_class"),
            "span_before": (p.get("span_before") or "")[:80],
        }
        for p in proposals
        if p.get("verdict") in (VERDICT_AUTO_CORRECT, VERDICT_AUTO_DELETE)
        and any(n in f"{p.get('span_before')} {p.get('evidence')}" for n in fact_needles)
    ]

    return {
        "job_id": job_id,
        "mode": mode,
        "pipeline_editor_version": PIPELINE_EDITOR_VERSION,
        "verdict_counts": verdict_counts,
        "fact_class_counts": fact_class_counts,
        "proposal_total": len(proposals),
        "fact_class_guard_downgrades": guard_downgrades,
        "fact_class_guard_downgrade_count": len(guard_downgrades),
        "gate_simulation": {
            "auto_proposal_count": auto_count,
            "would_fail": not gate.ok,
            "would_fail_rate": would_fail_rate,
            "violations": gate.violations,
            "warnings": gate.warnings,
        },
        "spot_checks": {
            "girigirioka": _find_examples(["義理をか", "義理"]),
            "garble_phrases": _find_examples(
                ["こうかだからな", "16時にちょっという", "ちょっという"]
            ),
            "fact_tokens_in_auto_verdict": fact_in_auto,
        },
        "gate_pass": gate.ok,
    }


def run_contextual_editor(
    job_dir: str,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    resolved_mode = resolve_editor_mode(mode)
    mechanical_path = os.path.join(job_dir, INPUT_MECHANICAL)
    if not os.path.isfile(mechanical_path):
        raise FileNotFoundError(f"mechanical transcript not found: {mechanical_path}")

    with open(mechanical_path, "r", encoding="utf-8") as f:
        text = f.read()

    profile = load_meeting_profile(job_dir)
    build_info = get_pipeline_build_info()
    job_id = os.path.basename(job_dir)

    if resolved_mode == "off":
        return {"skipped": True, "reason": "mode_off", "job_id": job_id}

    raw_proposals = _call_opus_for_proposals(text, profile)
    enriched = [
        _enrich_proposal(item, i + 1, text, meeting_profile=profile)
        for i, item in enumerate(raw_proposals)
    ]

    proposals_path = os.path.join(job_dir, EDIT_PROPOSALS_FILENAME)
    proposals_doc = {
        "job_id": job_id,
        "source": EDITOR_SOURCE,
        "model": CONTEXTUAL_EDITOR_MODEL,
        "pipeline_editor_version": PIPELINE_EDITOR_VERSION,
        "pipeline_correction_version": build_info["pipeline_correction_version"],
        "git_commit": build_info["git_commit"],
        "mode": resolved_mode,
        "input_path": INPUT_MECHANICAL,
        "input_chars": len(text),
        "proposals": enriched,
    }
    with open(proposals_path, "w", encoding="utf-8") as f:
        json.dump(proposals_doc, f, ensure_ascii=False, indent=2)

    shadow_report = _build_shadow_report(
        job_id=job_id,
        mode=resolved_mode,
        text=text,
        proposals=enriched,
        meeting_profile=profile,
    )
    report_path = os.path.join(job_dir, EDITOR_SHADOW_REPORT_FILENAME)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(shadow_report, f, ensure_ascii=False, indent=2)

    body_changed = False
    if resolved_mode == "apply":
        print("contextual_editor: apply mode not enabled in Phase 10.1")

    return {
        "job_id": job_id,
        "mode": resolved_mode,
        "proposal_total": len(enriched),
        "verdict_counts": shadow_report.get("verdict_counts"),
        "fact_class_guard_downgrade_count": shadow_report.get("fact_class_guard_downgrade_count"),
        "gate_would_fail": shadow_report.get("gate_simulation", {}).get("would_fail"),
        "body_changed": body_changed,
        "edit_proposals_path": proposals_path,
        "editor_shadow_report_path": report_path,
        "spot_checks": shadow_report.get("spot_checks"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Step 4.25 Contextual editor (Phase 10): full-text edit proposals"
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument(
        "--mode",
        choices=("shadow", "apply", "off"),
        default=None,
        help="Override CONTEXTUAL_EDITOR_MODE (default: env or shadow)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when CONTEXTUAL_EDITOR_ENABLED is off",
    )
    args = parser.parse_args()

    if not args.force and not is_contextual_editor_enabled():
        print(json.dumps({"skipped": True, "reason": "CONTEXTUAL_EDITOR_ENABLED is off"}))
        return 0

    job_dir = os.path.join(args.input_root, args.job_id)
    if not os.path.isdir(job_dir):
        print(f"job_dir not found: {job_dir}", file=sys.stderr)
        return 1

    try:
        result = run_contextual_editor(job_dir, mode=args.mode)
    except Exception as e:  # noqa: BLE001
        print(f"contextual_editor_failed: {e!r}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
