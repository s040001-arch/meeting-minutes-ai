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
    FACT_FILLER_GARBLE,
    INPUT_MECHANICAL,
    VERDICT_ASK_WITH_CANDIDATE,
    VERDICT_ASK_WITHOUT_CANDIDATE,
    VERDICT_AUTO_CORRECT,
    VERDICT_AUTO_DELETE,
    audit_garble_span,
    enforce_fact_routing,
    enforce_ambiguous_lexical_auto_correct,
    enforce_proper_noun_immutability,
    align_proposal_spans_in_text,
    new_proposal_id,
    normalize_fact_class,
    normalize_verdict,
    remediate_empty_auto_correct,
    summarize_garble_audits,
    to_unknown_point,
)
from editor_apply import (
    EDITOR_APPLY_REPORT_FILENAME,
    OUTPUT_AI_TRANSCRIPT,
    apply_proposals_with_gate,
)
from fact_classify import reclassify_proposal
from filler_garble_expand import expand_filler_garble_proposals
from fact_integrity_gate import simulate_apply_proposals, verify_fact_integrity
from semantic_integrity_gate import (
    is_semantic_integrity_gate_enabled,
    simulate_apply_with_dual_gates,
)
from meeting_profile import (
    augment_profile_with_transcript_participants,
    format_meeting_profile_for_prompt,
    load_meeting_profile,
)
from pipeline_build import get_pipeline_build_info
from repo_env import load_dotenv_local

CONTEXTUAL_EDITOR_MODEL = OPUS_MODEL_ID
CONTEXTUAL_EDITOR_MAX_TOKENS = 32000
CONTEXTUAL_EDITOR_TIMEOUT_SEC = 900
PIPELINE_EDITOR_VERSION = "20260615-phase10-2-4-shadow"

EDIT_PROPOSALS_FILENAME = "edit_proposals.json"
EDITOR_SHADOW_REPORT_FILENAME = "editor_shadow_report.json"

FULL_TEXT_CHAR_CAP = 60_000
ZERO_PROPOSAL_WARN_CHARS = 8000


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
        "\n- auto_correct (①): **同範囲の語置換のみ**。span_after に修正後の全文を必ず入れる。"
        "空の span_after で①を出すことは禁止（削除は④のみ）。"
        "reason に正しい語を書くなら span_after にも必ず反映せよ（reason/after 不一致禁止）。"
        "一般語・同音異義の誤りで文脈が明確。**事実に触れない**。"
        "フィラー・崩れ片には使わない（④へ）。"
        "\n- ask_with_candidate (②): 固有名詞・数値・決定等で、**全文文脈から候補を1つ示せる**疑い。"
        "hypothesis に候補語を必ず入れる。"
        "\n- ask_without_candidate (③): 聞くべきだが**文脈からも候補を出せない**場合のみ。"
        "\n- auto_delete (④): filler_garble のみ。"
        "フィラー・言い淀み・意味のない崩れ片・言い直し残骸・**読みやすさを損なう重複**を削除。"
        "**削除範囲は文脈判断で「残す価値のない最小の崩れ」のみ。**"
        "説明・論点・理由・手順・事実として読める発話を span に含めるな。"
        "\n\n【④で積極的に拾うノイズ（事実トークンが無い場合のみ）】"
        "\n- フィラー単独: えっと/あの/まあ/なんか 等が文脈に意味を足していない箇所"
        "\n- 完全・準完全な繰り返し: 「お願いします」連続、「いきなりいきなり」等、"
        "同じ語句の言い直し・吃音。話者が違っても挨拶・相槌の重複は削除してよい。"
        "\n- 言い直し残骸: 直後に同じ内容を言い直した前半の断片"
        "\n- 意味が加わらない反復は削除。強調・対比で内容が増える反復だけ残す。"
        "迷ったら「この重複で意味が増えるか」— 増えなければ④。"
        "\n\n【②と③の線引き（最重要）】"
        "\n- 全文を読んで正しい候補が推定できるなら **必ず② + hypothesis**。"
        "  例: 前後に「八戸」という東北の地名が並ぶ文脈で「義理をか」→ hypothesis=「盛岡」、"
        "verdict=ask_with_candidate。"
        "\n- ②は**確認のための候補提示**であり、事実の自動修正ではない。"
        "禁止しているのは「聞かずに候補で本文を書き換える①」だけ。"
        "事実系でも候補を出して②で相原に確認するのは正しい動作（捏造ではなく確認）。"
        "\n- 候補が本当に出せないときだけ③。"
        "\n\n【④ 削除境界 — 最重要（固定文字数ルールではなく文脈判断）】"
        "\n- garble_fragment: 消してよい最小の崩れ片。**必ず span_before 内の連続部分文字列**"
        "（span 外にあるのは矛盾）。"
        "\n- span_before: 削除適用範囲。**原文の連続部分文字列をそのまま正確に引用**。"
        "言い淀み・読点を省略・整形して引用するな。**原則 garble_fragment と完全一致**"
        "\n- preserve_rationale: なぜ span の前後（説明・論点）を残すか（叙述・1文）"
        "\n\n【span 引用 — ①②③④共通】"
        "\n- span_before は原文にそのまま存在する連続文字列のみ。"
        "言い淀み（「で、あ、」等）を飛ばして短く引用するな。"
        "\n- ① auto_correct: span_after は span_before と**同じ適用範囲**の修正版（必須・空禁止）。"
        "削除意図は④ auto_delete のみ。"
        "\n\n【① 反面教師 — 空 after 禁止】"
        "\n❌ 誤: verdict=auto_correct, span_after=\"\", reason=「構造勤務」は「工場勤務」の同音誤り"
        "  → 削除に化けている。①なら span_after に置換後を入れる。"
        "\n✅ 正: span_before=「全員構造勤務みたいな」, span_after=「全員工場勤務みたいな」"
        "\n❌ 誤: 施策説明「儲かりのフェアをやり…対策を議論していく」を④削除"
        "  → 意味ある発話は②③で聞く。迷ったら消さない。"
        "\n\n【④ 反面教師 — 過削除の例（「外いくつ」）】"
        "\n❌ 誤: span に「そもそも結構工程が多い…名簿読み込ませて…外いくつ」まで含める。"
        "  → 工程・名簿の説明は NEST の使いづらさを説明する**価値ある発話**。"
        "\n✅ 正: garble_fragment=span_before=「そこからさらに外いくつ」等、崩れ片のみ。"
        "preserve_rationale=「直前の工程・名簿説明は論点として残す」"
        "\n\n【④ 同型の注意】"
        "\n- 「合っています」: 残骸「、合っています」のみ。業務説明全体を span に含めない。"
        "\n- 「泳いか多く」: 崩れ片のみ。引き継ぎ文脈の説明まで巻き込まない。"
        "\n\n【④ auto_delete の良い例（filler_garble + auto_delete）】"
        "\n- 「これ、こうかだからな」— 文として崩れた言い直し残骸"
        "\n- 「16時にちょっという」「ちょっと 16 時にちょっという」— 途中切れ・言い淀みの崩れ片"
        "（確定の開催時刻の事実ではない）"
        "\n- 「あの、イメージングがちょっと動いてたりと」— 意味を持たないフィラー的断片"
        "\n- 「えーと」「あのー」だけの繰り返し、言い直しの重複残骸"
        "\n- 「お願いします。。。お願いします」— 挨拶・相槌の重複（後半側を削除）"
        "\n- 「いきなりいきなり」— 吃音・重複語の片方"
        "\n- 文頭・句読点直後の「えっと」「あの」— 意味を足さないフィラー単独"
        "\nフィラー・崩れ片は① auto_correct にしない。④で削除する。"
        "\n\n【鉄の掟】"
        "\n- 固有名詞・数値・金額・日時・決定事項は確信が高くても auto_correct / auto_delete 禁止。"
        "②または③へ（候補が出せれば②）。"
        "\n- ④は filler_garble のみ。金額・固有名詞・決定が1つでも含まれるスパンは④禁止。"
        "\n- 数値閾値で省略しない。聞くべき箇所は②③に載せる。"
        "\n\n【出力スキーマ】JSON 配列のみ。各要素:"
        '\n{"span_before":"削除/修正適用範囲（原文から正確に引用した連続部分文字列）",'
        '"span_after":"①のみ修正後（同範囲の修正版）。②③④は空文字",'
        '"garble_fragment":"④のみ: 消してよい最小崩れ（span_before内の部分文字列＝原則同一）。①②③は空",'
        '"preserve_rationale":"④のみ: 前後を残す理由。①②③は空",'
        '"verdict":"auto_correct|ask_with_candidate|ask_without_candidate|auto_delete",'
        '"fact_class":"lexical_fluency|filler_garble|proper_noun|numeric|datetime|decision|uncertain",'
        '"hypothesis":"②のみ候補語（1-15字）。それ以外は空",'
        '"evidence":"前後30-100字の引用",'
        '"importance":"なぜこの帰結か（叙述・1文）",'
        '"reason":"判定根拠（50字以内）",'
        '"anomaly_word":"スパン中心語（質問表示用）"}'
        "\n\n問題が無ければ [] を返す。"
        "**[] は誤認識・崩れが皆無と確信できる場合のみ。**"
        "長文でも逐語録を最後まで読み、編集対象を探せ。"
        "固有名詞・数値・同音異義・崩れ片の見落としを避ける。"
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


def _normalize_filler_verdict(proposal: dict[str, Any]) -> dict[str, Any]:
    """filler_garble mislabeled as auto_correct → auto_delete (safe nudge)."""
    if (
        proposal.get("fact_class") == FACT_FILLER_GARBLE
        and normalize_verdict(proposal.get("verdict")) == VERDICT_AUTO_CORRECT
    ):
        proposal["original_verdict"] = VERDICT_AUTO_CORRECT
        proposal["verdict"] = VERDICT_AUTO_DELETE
        proposal["span_after"] = ""
        proposal["routing_override"] = proposal.get("routing_override") or "filler_to_delete"
    return proposal


def _enrich_proposal(
    raw: dict,
    idx: int,
    text: str,
    *,
    meeting_profile: dict | None,
) -> dict[str, Any]:
    span_before = str(raw.get("span_before") or "").strip()
    span_after = str(raw.get("span_after") or "").strip()
    garble_fragment = str(raw.get("garble_fragment") or "").strip()
    preserve_rationale = str(raw.get("preserve_rationale") or "").strip()
    anomaly_word = str(raw.get("anomaly_word") or "").strip()
    if not anomaly_word and span_before:
        anomaly_word = span_before[: min(15, len(span_before))]

    proposal: dict[str, Any] = {
        "proposal_id": str(raw.get("proposal_id") or new_proposal_id()),
        "verdict": normalize_verdict(raw.get("verdict")),
        "span_before": span_before,
        "span_after": span_after,
        "garble_fragment": garble_fragment,
        "preserve_rationale": preserve_rationale[:300],
        "fact_class": normalize_fact_class(raw.get("fact_class")),
        "fact_class_source": "llm",
        "hypothesis": str(raw.get("hypothesis") or "").strip(),
        "evidence": str(raw.get("evidence") or "").strip()[:200],
        "importance": str(raw.get("importance") or "").strip()[:200],
        "reason": str(raw.get("reason") or "").strip()[:120],
        "anomaly_word": anomaly_word,
        "span_start": -1,
        "span_end": -1,
        "context_position_in_transcript": -1,
        "applied": False,
    }

    reclassify_proposal(proposal, meeting_profile=meeting_profile)
    _normalize_filler_verdict(proposal)
    remediate_empty_auto_correct(proposal)
    if normalize_verdict(proposal.get("verdict")) == VERDICT_AUTO_DELETE:
        if not proposal.get("garble_fragment") and proposal.get("anomaly_word"):
            proposal["garble_fragment"] = str(proposal.get("anomaly_word") or "")
        if not proposal.get("span_before") and proposal.get("garble_fragment"):
            proposal["span_before"] = str(proposal.get("garble_fragment") or "")

    align_proposal_spans_in_text(text, proposal)
    proposal["garble_span_audit"] = audit_garble_span(proposal)
    enforce_fact_routing(proposal)
    enforce_proper_noun_immutability(proposal, meeting_profile=meeting_profile)
    return proposal


def refresh_proposal_routing(
    proposals: list[dict[str, Any]],
    *,
    meeting_profile: dict | None,
    text: str | None = None,
) -> list[dict[str, Any]]:
    """Re-run span alignment and layer-1 routing guards on loaded proposals."""
    for proposal in proposals:
        remediate_empty_auto_correct(proposal)
        if text:
            align_proposal_spans_in_text(text, proposal)
            proposal["garble_span_audit"] = audit_garble_span(proposal)
        enforce_fact_routing(proposal)
        enforce_proper_noun_immutability(proposal, meeting_profile=meeting_profile)
    if text:
        for proposal in proposals:
            enforce_ambiguous_lexical_auto_correct(
                proposal,
                text=text,
                peer_proposals=proposals,
            )
        already_expanded = any(
            p.get("supplemental_filler_expand") for p in proposals
        )
        supplemental = (
            []
            if already_expanded
            else expand_filler_garble_proposals(
                text, proposals, meeting_profile=meeting_profile
            )
        )
        if supplemental:
            for proposal in supplemental:
                align_proposal_spans_in_text(text, proposal)
                proposal["garble_span_audit"] = audit_garble_span(proposal)
                enforce_fact_routing(proposal)
                enforce_proper_noun_immutability(
                    proposal, meeting_profile=meeting_profile
                )
            proposals.extend(supplemental)
    return proposals


def _merge_editor_questions_to_unknown_points(
    job_dir: str,
    proposals: list[dict[str, Any]],
) -> int:
    """Append ②③ editor proposals to unknown_points.json (no LINE send)."""
    pending = [
        p
        for p in proposals
        if normalize_verdict(p.get("verdict"))
        in (VERDICT_ASK_WITH_CANDIDATE, VERDICT_ASK_WITHOUT_CANDIDATE)
    ]
    if not pending:
        return 0
    path = os.path.join(job_dir, "unknown_points.json")
    existing: list[dict] = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                existing = [x for x in data if isinstance(x, dict)]
        except (OSError, json.JSONDecodeError):
            existing = []
    existing_ids = {
        str(it.get("anomaly_id") or "") for it in existing if it.get("anomaly_id")
    }
    added = 0
    for p in pending:
        pid = str(p.get("proposal_id") or "")
        if pid in existing_ids:
            continue
        try:
            existing.append(to_unknown_point(p))
        except ValueError:
            continue
        added += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return added


def _apply_proposals_to_job(
    job_dir: str,
    text: str,
    proposals: list[dict[str, Any]],
    *,
    meeting_profile: dict | None,
) -> dict[str, Any]:
    out_text, applied, reverted_fact, skipped, reverted_semantic = apply_proposals_with_gate(
        text,
        proposals,
        meeting_profile=meeting_profile,
        run_semantic=is_semantic_integrity_gate_enabled(),
    )
    gate = verify_fact_integrity(text, out_text, meeting_profile=meeting_profile)
    reverted = reverted_fact + reverted_semantic

    apply_entries: list[dict[str, Any]] = []
    for p in applied:
        entry: dict[str, Any] = {
            "proposal_id": p.get("proposal_id"),
            "verdict": p.get("verdict"),
            "fact_class": p.get("fact_class"),
            "span_before": p.get("span_before"),
            "span_after": p.get("span_after") or "",
            "anomaly_word": p.get("anomaly_word"),
        }
        apply_entries.append(entry)

    ai_path = os.path.join(job_dir, OUTPUT_AI_TRANSCRIPT)
    with open(ai_path, "w", encoding="utf-8") as f:
        f.write(out_text)

    after_qa_path = os.path.join(job_dir, "merged_transcript_after_qa.txt")
    with open(after_qa_path, "w", encoding="utf-8") as f:
        f.write(out_text)

    questions_added = _merge_editor_questions_to_unknown_points(job_dir, proposals)

    report = {
        "input_path": INPUT_MECHANICAL,
        "output_path": OUTPUT_AI_TRANSCRIPT,
        "input_chars": len(text),
        "output_chars": len(out_text),
        "applied_count": len(applied),
        "reverted_count": len(reverted),
        "fact_reverted_count": len(reverted_fact),
        "semantic_reverted_count": len(reverted_semantic),
        "skipped_count": len(skipped),
        "applied": apply_entries,
        "reverted": [
            {
                "proposal_id": p.get("proposal_id"),
                "verdict": p.get("verdict"),
                "apply_error": p.get("apply_error"),
                "revert_layer": p.get("revert_layer"),
                "semantic_check": p.get("semantic_check"),
                "span_before": (p.get("span_before") or "")[:120],
                "garble_span_synced": p.get("garble_span_synced"),
            }
            for p in reverted
        ],
        "skipped": [
            {
                "proposal_id": p.get("proposal_id"),
                "verdict": p.get("verdict"),
                "apply_error": p.get("apply_error"),
            }
            for p in skipped
        ],
        "gate_final": {
            "ok": gate.ok,
            "violations": gate.violations,
        },
        "questions_merged_to_unknown_points": questions_added,
        "remaining_questions": sum(
            1
            for p in proposals
            if normalize_verdict(p.get("verdict"))
            in (VERDICT_ASK_WITH_CANDIDATE, VERDICT_ASK_WITHOUT_CANDIDATE)
        ),
    }
    report_path = os.path.join(job_dir, EDITOR_APPLY_REPORT_FILENAME)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


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

    garble_audit = summarize_garble_audits(proposals)

    empty_auto_correct_remediations: list[dict[str, Any]] = []
    empty_auto_correct_remaining = 0
    for p in proposals:
        rem = str(p.get("empty_auto_correct_remediation") or "")
        if rem:
            empty_auto_correct_remediations.append(
                {
                    "proposal_id": p.get("proposal_id"),
                    "remediation": rem,
                    "verdict": p.get("verdict"),
                    "span_before": (p.get("span_before") or "")[:80],
                    "span_after": (p.get("span_after") or "")[:80],
                    "hypothesis": p.get("hypothesis"),
                }
            )
        if (
            normalize_verdict(p.get("verdict")) == VERDICT_AUTO_CORRECT
            and not str(p.get("span_after") or "").strip()
        ):
            empty_auto_correct_remaining += 1

    shadow_warnings: list[dict[str, Any]] = []
    if len(proposals) == 0 and len(text) > ZERO_PROPOSAL_WARN_CHARS:
        shadow_warnings.append(
            {
                "code": "zero_proposals_long_transcript",
                "text_chars": len(text),
                "threshold_chars": ZERO_PROPOSAL_WARN_CHARS,
                "message": (
                    "Opus returned no proposals for a long transcript; "
                    "verify recognition issues were not missed."
                ),
            }
        )
    if empty_auto_correct_remaining:
        shadow_warnings.append(
            {
                "code": "empty_auto_correct_remaining",
                "count": empty_auto_correct_remaining,
                "message": "auto_correct proposals still have empty span_after after enrich.",
            }
        )

    def _garble_spot(needles: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for needle in needles:
            for p in proposals:
                if p.get("verdict") != VERDICT_AUTO_DELETE:
                    continue
                if needle not in f"{p.get('span_before')} {p.get('garble_fragment')} {p.get('anomaly_word')}":
                    continue
                audit = p.get("garble_span_audit") or audit_garble_span(p)
                out.append(
                    {
                        "needle": needle,
                        "proposal_id": p.get("proposal_id"),
                        "span_before": p.get("span_before"),
                        "garble_fragment": p.get("garble_fragment"),
                        "preserve_rationale": p.get("preserve_rationale"),
                        "garble_fragment_match": audit.get("garble_fragment_match"),
                        "span_overflow_flags": audit.get("span_overflow_flags"),
                    }
                )
                break
        return out

    semantic_sim: dict[str, Any] = {"enabled": False}
    if is_semantic_integrity_gate_enabled():
        try:
            semantic_sim = simulate_apply_with_dual_gates(
                text,
                proposals,
                meeting_profile=meeting_profile,
                run_semantic=True,
            )
            semantic_sim["enabled"] = True
        except Exception as e:  # noqa: BLE001
            semantic_sim = {"enabled": True, "error": repr(e)}

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
            "garble_minimal_span": _garble_spot(
                ["外いくつ", "合っています", "泳いか多く"]
            ),
            "fact_tokens_in_auto_verdict": fact_in_auto,
        },
        "garble_span_audit": garble_audit,
        "empty_auto_correct_remediation": {
            "count": len(empty_auto_correct_remediations),
            "remaining_empty_auto_correct": empty_auto_correct_remaining,
            "items": empty_auto_correct_remediations,
        },
        "shadow_warnings": shadow_warnings,
        "semantic_gate_simulation": semantic_sim,
        "gate_pass": gate.ok,
    }


def run_contextual_editor(
    job_dir: str,
    *,
    mode: str | None = None,
    apply_only: bool = False,
) -> dict[str, Any]:
    resolved_mode = resolve_editor_mode(mode)
    mechanical_path = os.path.join(job_dir, INPUT_MECHANICAL)
    if not os.path.isfile(mechanical_path):
        raise FileNotFoundError(f"mechanical transcript not found: {mechanical_path}")

    with open(mechanical_path, "r", encoding="utf-8") as f:
        text = f.read()

    profile = augment_profile_with_transcript_participants(
        load_meeting_profile(job_dir), text
    )
    build_info = get_pipeline_build_info()
    job_id = os.path.basename(job_dir)

    if resolved_mode == "off":
        return {"skipped": True, "reason": "mode_off", "job_id": job_id}

    proposals_path = os.path.join(job_dir, EDIT_PROPOSALS_FILENAME)

    if apply_only and os.path.isfile(proposals_path):
        with open(proposals_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        enriched = list(doc.get("proposals") or [])
        refresh_proposal_routing(enriched, meeting_profile=profile, text=text)
    else:
        raw_proposals = _call_opus_for_proposals(text, profile)
        enriched = [
            _enrich_proposal(item, i + 1, text, meeting_profile=profile)
            for i, item in enumerate(raw_proposals)
        ]
        refresh_proposal_routing(enriched, meeting_profile=profile, text=text)

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
    apply_report: dict[str, Any] | None = None
    if resolved_mode == "apply":
        apply_report = _apply_proposals_to_job(
            job_dir, text, enriched, meeting_profile=profile
        )
        body_changed = apply_report.get("applied_count", 0) > 0

    result: dict[str, Any] = {
        "job_id": job_id,
        "mode": resolved_mode,
        "apply_only": apply_only,
        "proposal_total": len(enriched),
        "verdict_counts": shadow_report.get("verdict_counts"),
        "fact_class_guard_downgrade_count": shadow_report.get("fact_class_guard_downgrade_count"),
        "gate_would_fail": shadow_report.get("gate_simulation", {}).get("would_fail"),
        "body_changed": body_changed,
        "edit_proposals_path": proposals_path,
        "editor_shadow_report_path": report_path,
        "spot_checks": shadow_report.get("spot_checks"),
        "garble_span_audit": shadow_report.get("garble_span_audit"),
        "semantic_gate_simulation": shadow_report.get("semantic_gate_simulation"),
    }
    if apply_report:
        result["apply_report"] = apply_report
        result["editor_apply_report_path"] = os.path.join(job_dir, EDITOR_APPLY_REPORT_FILENAME)
        result["output_ai_path"] = os.path.join(job_dir, OUTPUT_AI_TRANSCRIPT)
    return result


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
    parser.add_argument(
        "--apply-only",
        action="store_true",
        help="Apply existing edit_proposals.json without calling Opus (apply mode)",
    )
    args = parser.parse_args()

    load_dotenv_local()

    if not args.force and not is_contextual_editor_enabled():
        print(json.dumps({"skipped": True, "reason": "CONTEXTUAL_EDITOR_ENABLED is off"}))
        return 0

    job_dir = os.path.join(args.input_root, args.job_id)
    if not os.path.isdir(job_dir):
        print(f"job_dir not found: {job_dir}", file=sys.stderr)
        return 1

    try:
        result = run_contextual_editor(
            job_dir, mode=args.mode, apply_only=args.apply_only
        )
    except Exception as e:  # noqa: BLE001
        print(f"contextual_editor_failed: {e!r}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
