"""Generic integrated question MD + cascade (波及) grouping for any job.

Combines:
  - contextual_editor ②③ (edit_proposals.json ask_* verdicts)
  - reader_pass findings (reader_pass_result.json or reader_pass_questions.md)

Cascade groups: one answer lands on multiple spans (same hypothesis).
Uses question_bundle.bundle_safe_answer_items — no job-specific hardcoding.

Outputs:
  {job_dir}/questions_review.md   (Cursor 疑似運用)
  {job_dir}/integrated_questions.json  (machine-readable bundles)

LINE (QUESTION_MODE=line): select_next_line_bundle() builds one push payload
with targets[] so recorrect/pinpoint can apply all spans from one answer.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from edit_proposal_schema import (
    VERDICT_ASK_WITH_CANDIDATE,
    VERDICT_ASK_WITHOUT_CANDIDATE,
    normalize_verdict,
)
from question_bundle import (
    build_bundled_replace_question_text,
    build_single_replace_question_text,
    bundle_safe_answer_items,
    normalize_hypothesis,
)

INTEGRATED_JSON = "integrated_questions.json"
QUESTIONS_REVIEW_MD = "questions_review.md"
ASK_VERDICTS = frozenset({VERDICT_ASK_WITH_CANDIDATE, VERDICT_ASK_WITHOUT_CANDIDATE})


# ── loaders ──────────────────────────────────────────────────────────────────

def load_ask_proposals(job_dir: Path) -> list[dict[str, Any]]:
    path = job_dir / "edit_proposals.json"
    if not path.is_file():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    proposals = doc.get("proposals") if isinstance(doc, dict) else None
    if not isinstance(proposals, list):
        return []
    out: list[dict[str, Any]] = []
    for p in proposals:
        if not isinstance(p, dict):
            continue
        if normalize_verdict(p.get("verdict")) not in ASK_VERDICTS:
            continue
        if p.get("applied") is True:
            continue
        out.append(p)
    return out


def load_reader_pass_findings(job_dir: Path) -> list[dict[str, Any]]:
    result_path = job_dir / "reader_pass_result.json"
    if result_path.is_file():
        try:
            doc = json.loads(result_path.read_text(encoding="utf-8"))
            findings = doc.get("findings") if isinstance(doc, dict) else None
            if isinstance(findings, list):
                return [f for f in findings if isinstance(f, dict)]
        except (OSError, json.JSONDecodeError):
            pass
    md_path = job_dir / "reader_pass_questions.md"
    if md_path.is_file():
        return _parse_reader_pass_md(md_path)
    return []


def _parse_reader_pass_md(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    items: list[dict[str, Any]] = []
    blocks = re.split(r"^## #(\d+)", text, flags=re.MULTILINE)
    i = 1
    while i < len(blocks) - 1:
        rank = int(blocks[i])
        body = blocks[i + 1]
        lines = body.strip().splitlines()
        title = lines[0].strip() if lines else ""
        excerpt = _extract_between(body, r"\*\*該当テキスト\*\*", r"\*\*なぜ", strip_blockquote=True)
        reason = _extract_between(body, r"\*\*なぜ分からないか\*\*", r"\*\*質問\*\*")
        question = _extract_between(body, r"\*\*質問\*\*", r"→ 回答:")
        answer_m = re.search(r"→ 回答:(.*?)(?:---|$)", body, re.DOTALL)
        answer = answer_m.group(1).strip() if answer_m else ""
        items.append({
            "rank": rank,
            "title": title,
            "excerpt": excerpt,
            "reason": reason,
            "question": question,
            "answer": answer,
        })
        i += 2
    return items


def _extract_between(
    text: str, start_pattern: str, end_pattern: str, *, strip_blockquote: bool = False
) -> str:
    m = re.search(start_pattern + r"\s*\n(.*?)(?=" + end_pattern + ")", text, re.DOTALL)
    if not m:
        return ""
    raw = m.group(1).strip()
    if strip_blockquote:
        raw = re.sub(r"^> ?", "", raw, flags=re.MULTILINE).strip()
    return raw


def load_transcript(job_dir: Path) -> str:
    for name in (
        "merged_transcript_after_qa.txt",
        "merged_transcript_ai.txt",
        "ai_with_notes.txt",
        "merged_transcript_mechanical.txt",
    ):
        p = job_dir / name
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return ""


# ── cascade / bundle ─────────────────────────────────────────────────────────

def proposal_to_bundle_item(p: dict[str, Any], *, review_index: int) -> dict[str, Any]:
    verdict = normalize_verdict(p.get("verdict"))
    return {
        "proposal_id": p.get("proposal_id"),
        "anomaly_word": p.get("anomaly_word"),
        "span_before": p.get("span_before") or p.get("evidence") or "",
        "span_start": p.get("span_start") if p.get("span_start") is not None
        else p.get("context_position_in_transcript", -1),
        "hypothesis": p.get("hypothesis") or "",
        "fact_class": p.get("fact_class"),
        "context": p.get("evidence") or p.get("context") or "",
        "reason": p.get("reason") or p.get("importance") or "",
        "importance": p.get("importance"),
        "review_index": review_index,
        "verdict": verdict,
        "answer_text": "",
        "selected_unknown": {
            "type": "contextual_editor",
            "source": "contextual_editor",
            "verdict": verdict,
            "anomaly_word": p.get("anomaly_word"),
            "hypothesis": p.get("hypothesis") or "",
            "span_text": p.get("span_before") or "",
            "fact_class": p.get("fact_class"),
        },
    }


def priority_key(item: dict[str, Any]) -> tuple:
    fc = str(item.get("fact_class") or "")
    verdict = normalize_verdict(
        (item.get("selected_unknown") or {}).get("verdict") or item.get("verdict")
    )
    if fc == "proper_noun" and verdict == VERDICT_ASK_WITH_CANDIDATE:
        return (1, 0)
    if fc == "proper_noun":
        return (2, 0)
    if fc == "lexical_fluency":
        return (3, 0)
    return (4, 0)


def build_cascade_questions(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return review questions with cascade groups auto-detected.

    Each entry:
      review_index, question_text, hypothesis, targets[], cascade_note,
      anomaly_words[], fact_class, verdict, is_bundle
    """
    if not proposals:
        return []
    items = [
        proposal_to_bundle_item(p, review_index=i + 1)
        for i, p in enumerate(proposals)
    ]
    # Only ② (with candidate / shared hypothesis) go through safe bundle.
    with_cand = [
        it for it in items
        if normalize_verdict((it.get("selected_unknown") or {}).get("verdict"))
        == VERDICT_ASK_WITH_CANDIDATE
        and normalize_hypothesis(str(it.get("hypothesis") or ""))
    ]
    without = [it for it in items if it not in with_cand]

    bundled = bundle_safe_answer_items(with_cand)

    # Secondary cascade: identical anomaly_word with empty hypothesis (same garble, many spans)
    by_word: dict[str, list[dict[str, Any]]] = {}
    still_without: list[dict[str, Any]] = []
    for it in without:
        word = str(it.get("anomaly_word") or "").strip()
        hyp = normalize_hypothesis(str(it.get("hypothesis") or ""))
        if word and not hyp:
            by_word.setdefault(word, []).append(it)
        else:
            still_without.append(it)
    for word, group in by_word.items():
        if len(group) >= 2:
            first = dict(group[0])
            first["targets"] = [dict(g) for g in group]
            first["hypothesis"] = ""
            first["question_text"] = build_bundled_replace_question_text(first)
            first["is_word_cascade"] = True
            bundled.append(first)
        else:
            still_without.extend(group)
    without = still_without

    questions: list[dict[str, Any]] = []

    for entry in bundled:
        targets = entry.get("targets") if isinstance(entry.get("targets"), list) else None
        if targets and len(targets) >= 2:
            ids = [str(t.get("review_index") or "") for t in targets]
            words = [str(t.get("anomaly_word") or "") for t in targets]
            primary = ids[0]
            others = [i for i in ids[1:] if i]
            cascade_note = (
                f"この回答で B-{primary}"
                + (f" と " + "・".join(f"B-{o}" for o in others) if others else "")
                + " も確定します"
            )
            questions.append({
                "review_index": entry.get("review_index"),
                "question_text": entry.get("question_text") or build_bundled_replace_question_text(entry),
                "hypothesis": entry.get("hypothesis") or "",
                "targets": targets,
                "cascade_note": cascade_note,
                "anomaly_words": words,
                "fact_class": entry.get("fact_class"),
                "verdict": VERDICT_ASK_WITH_CANDIDATE,
                "is_bundle": True,
                "proposal_ids": [t.get("proposal_id") for t in targets],
            })
        else:
            questions.append({
                "review_index": entry.get("review_index"),
                "question_text": entry.get("question_text") or build_single_replace_question_text(entry),
                "hypothesis": entry.get("hypothesis") or "",
                "targets": [entry],
                "cascade_note": "",
                "anomaly_words": [str(entry.get("anomaly_word") or "")],
                "fact_class": entry.get("fact_class"),
                "verdict": VERDICT_ASK_WITH_CANDIDATE,
                "is_bundle": False,
                "proposal_ids": [entry.get("proposal_id")],
            })

    for it in without:
        questions.append({
            "review_index": it.get("review_index"),
            "question_text": build_single_replace_question_text(it),
            "hypothesis": it.get("hypothesis") or "",
            "targets": [it],
            "cascade_note": "",
            "anomaly_words": [str(it.get("anomaly_word") or "")],
            "fact_class": it.get("fact_class"),
            "verdict": normalize_verdict(
                (it.get("selected_unknown") or {}).get("verdict")
            ),
            "is_bundle": False,
            "proposal_ids": [it.get("proposal_id")],
        })

    questions.sort(key=lambda q: (
        priority_key(q["targets"][0] if q.get("targets") else {}),
        int(q.get("review_index") or 0),
    ))
    # Re-number B-n in display order
    for i, q in enumerate(questions, start=1):
        q["display_index"] = i
    return questions


def _context_snippet(transcript: str, pos: int, window: int = 80) -> str:
    if not transcript or pos is None or int(pos) < 0:
        return ""
    pos = int(pos)
    start = max(0, pos - 15)
    end = min(len(transcript), pos + window)
    return transcript[start:end].replace("\n", " ").strip()


# ── MD generation ────────────────────────────────────────────────────────────

def build_integrated_md(
    *,
    job_id: str,
    reader_findings: list[dict[str, Any]],
    cascade_questions: list[dict[str, Any]],
    transcript: str,
    resume_hint: str = "",
) -> tuple[str, dict[str, Any]]:
    raw_spans = sum(len(q.get("targets") or []) for q in cascade_questions)
    bundle_count = sum(1 for q in cascade_questions if q.get("is_bundle"))
    stats = {
        "job_id": job_id,
        "reader_pass_count": len(reader_findings),
        "ask_raw_spans": raw_spans,
        "ask_questions": len(cascade_questions),
        "cascade_groups": bundle_count,
        "cascade_span_savings": max(0, raw_spans - len(cascade_questions)),
    }

    lines: list[str] = [
        f"# 統合質問シート",
        "",
        f"- job: `{job_id}`",
        f"- Section A (reader pass): **{stats['reader_pass_count']}** 件",
        (
            f"- Section B (②③): **{stats['ask_raw_spans']}** span → "
            f"**{stats['ask_questions']}** 問"
            f"（波及グループ {stats['cascade_groups']}、"
            f"{stats['cascade_span_savings']} 問削減）"
        ),
        "",
    ]
    if resume_hint:
        lines += ["回答・本文確定後:", "", "```", resume_hint, "```", ""]
    lines += ["---", ""]

    # Section A
    lines += ["## Section A — Reader pass", ""]
    if not reader_findings:
        lines += ["_reader pass 所見なし_", "", "---", ""]
    else:
        for f in reader_findings:
            rank = f.get("rank", "?")
            title = f.get("title") or f.get("question") or ""
            excerpt = f.get("excerpt") or ""
            question = f.get("question") or ""
            answer = str(f.get("answer") or "").strip()
            reason = f.get("reason") or ""
            lines += [f"### A-{rank}　{title}", ""]
            if excerpt:
                lines += [f"**該当テキスト**: {excerpt}", ""]
            if reason:
                lines += [f"**なぜ**: {reason}", ""]
            if question:
                lines += [f"**質問**: {question}", ""]
            if answer:
                lines += [f"**確認済み回答**: {answer}", ""]
            else:
                lines += ["→ 回答:", ""]
            lines += ["---", ""]

    # Section B
    lines += [
        "## Section B — ②③（波及グループ自動検出）",
        "",
        "> 同じ着地（hypothesis）の複数 span は 1 問にまとめ、"
        "> 「この回答で B-n も確定します」と注記します。",
        "",
    ]
    if not cascade_questions:
        lines += ["_②③ ask 提案なし_", ""]
    else:
        for q in cascade_questions:
            di = q.get("display_index") or q.get("review_index")
            words = " / ".join(f"`{w}`" for w in q.get("anomaly_words") or [] if w)
            hyp = q.get("hypothesis") or ""
            targets = q.get("targets") or []
            pos = targets[0].get("span_start", -1) if targets else -1
            ctx = _context_snippet(transcript, pos)
            header = f"### B-{di}"
            if q.get("is_bundle"):
                header += f"　【バンドル】{words}"
            else:
                aw = (q.get("anomaly_words") or [""])[0]
                header += f"　`{aw}`"
                if hyp:
                    header += f" → `{hyp}`"
            lines += [header, ""]
            verdict = q.get("verdict") or ""
            kind = "② ask_with_candidate" if verdict == VERDICT_ASK_WITH_CANDIDATE else "③ ask_without_candidate"
            lines += [f"**種別**: {kind}"]
            if words:
                lines += [f"**バリアント**: {words}"]
            if hyp:
                lines += [f"**修正候補**: `{hyp}`"]
            if ctx:
                lines += ["", "**代表コンテキスト**:", f"> …{ctx}…", ""]
            if q.get("cascade_note"):
                lines += [f"> ⚠️ 波及: {q['cascade_note']}", ""]
            lines += ["→ 回答:", "", "---", ""]

    lines += [
        "",
        f"_integrated_questions / ask_raw={stats['ask_raw_spans']} "
        f"questions={stats['ask_questions']} cascade_groups={stats['cascade_groups']}_",
        "",
    ]
    return "\n".join(lines), stats


def write_integrated_questions(
    job_dir: str | Path,
    *,
    job_id: str = "",
    resume_hint: str = "",
) -> tuple[Path, dict[str, Any]]:
    job = Path(job_dir)
    jid = job_id or job.name
    proposals = load_ask_proposals(job)
    findings = load_reader_pass_findings(job)
    transcript = load_transcript(job)
    cascade = build_cascade_questions(proposals)
    if not resume_hint:
        resume_hint = f"python reprocess_job.py --job-dir {job} --from-step resume"
    md, stats = build_integrated_md(
        job_id=jid,
        reader_findings=findings,
        cascade_questions=cascade,
        transcript=transcript,
        resume_hint=resume_hint,
    )
    md_path = job / QUESTIONS_REVIEW_MD
    md_path.write_text(md, encoding="utf-8")

    payload = {
        "job_id": jid,
        "stats": stats,
        "reader_pass": findings,
        "questions": cascade,
    }
    json_path = job / INTEGRATED_JSON
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    stats["md_path"] = str(md_path)
    stats["json_path"] = str(json_path)
    return md_path, stats


# ── LINE bundle (QUESTION_MODE=line) ─────────────────────────────────────────

def select_next_line_bundle(job_dir: str | Path) -> dict[str, Any] | None:
    """Pick highest-priority unanswered cascade question for one LINE push."""
    job = Path(job_dir)
    proposals = load_ask_proposals(job)
    if not proposals:
        return None
    cascade = build_cascade_questions(proposals)
    if not cascade:
        return None
    # Prefer bundles (more value per message), then priority order already applied
    bundles = [q for q in cascade if q.get("is_bundle")]
    chosen = bundles[0] if bundles else cascade[0]
    targets = chosen.get("targets") or []
    primary = targets[0] if targets else {}
    question_id = str(uuid.uuid4())
    selected_unknown = {
        "type": "contextual_editor",
        "source": "contextual_editor",
        "verdict": chosen.get("verdict") or VERDICT_ASK_WITH_CANDIDATE,
        "anomaly_word": primary.get("anomaly_word"),
        "hypothesis": chosen.get("hypothesis") or "",
        "span_text": primary.get("span_before") or "",
        "span_before": primary.get("span_before") or "",
        "fact_class": chosen.get("fact_class"),
        "targets": targets,
        "bundle_kind": "replace" if chosen.get("is_bundle") else None,
        "cascade_note": chosen.get("cascade_note") or "",
    }
    return {
        "job_id": job.name,
        "question_id": question_id,
        "question_status": "generated",
        "question_format": "bundle" if chosen.get("is_bundle") else "free_text",
        "question_text": chosen.get("question_text") or "",
        "selected_unknown": selected_unknown,
        "targets": targets,
        "hypothesis": chosen.get("hypothesis") or "",
        "cascade_note": chosen.get("cascade_note") or "",
        "is_bundle": bool(chosen.get("is_bundle")),
        "display_index": chosen.get("display_index"),
        "message": "",
        "doc_url": _load_doc_url(job),
        "selection_audit": {
            "selection_mode": "integrated_cascade_bundle",
            "cascade_note": chosen.get("cascade_note") or "",
            "target_count": len(targets),
        },
    }


def _load_doc_url(job_dir: Path) -> str:
    hub = job_dir / "google_doc_hub.json"
    if not hub.is_file():
        return ""
    try:
        data = json.loads(hub.read_text(encoding="utf-8"))
        doc_id = str(data.get("doc_id") or "").strip()
        if doc_id:
            return f"https://docs.google.com/document/d/{doc_id}/edit"
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def write_and_maybe_push_line_bundle(
    job_dir: str | Path,
    *,
    send_line: bool,
) -> dict[str, Any]:
    """Write question_result / message for cascade bundle; optionally push LINE."""
    from line_send_question import build_line_message, push_line_message
    from run_question_cycle_once import write_line_pending_context

    job = Path(job_dir)
    payload = select_next_line_bundle(job)
    result: dict[str, Any] = {"sent": False, "reason": "no_bundle"}
    if payload is None:
        return result

    q_path = job / "question_result.json"
    msg_path = job / "question_message.txt"
    q_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    message_text = build_line_message(payload)
    # Append cascade note for human clarity on LINE
    note = str(payload.get("cascade_note") or "").strip()
    if note and note not in message_text:
        message_text = message_text.rstrip() + f"\n\n（{note}）"
    msg_path.write_text(message_text, encoding="utf-8")

    write_line_pending_context(
        job_id=job.name,
        question_id=str(payload.get("question_id") or ""),
        question_text=str(payload.get("question_text") or ""),
        selected_unknown=payload.get("selected_unknown") or {},
        selection_audit=payload.get("selection_audit") or {},
    )

    result = {
        "sent": False,
        "reason": "written_only",
        "question_id": payload.get("question_id"),
        "is_bundle": payload.get("is_bundle"),
        "target_count": len(payload.get("targets") or []),
        "message_path": str(msg_path),
    }
    if not send_line:
        return result

    import os

    user_id = os.getenv("LINE_USER_ID", "").strip()
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not user_id or not token:
        result["reason"] = "line_credentials_missing"
        return result
    try:
        push_line_message(
            channel_access_token=token,
            user_id=user_id,
            text=message_text,
        )
        result["sent"] = True
        result["reason"] = "sent_bundle" if payload.get("is_bundle") else "sent_single"
    except Exception as e:  # noqa: BLE001
        result["reason"] = f"push_failed:{e!r}"
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="統合質問MD生成（全ジョブ共通）")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--job-id", default="")
    args = parser.parse_args()
    path, stats = write_integrated_questions(args.job_dir, job_id=args.job_id)
    print(f"wrote {path}")
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
