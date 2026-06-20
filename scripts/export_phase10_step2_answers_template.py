#!/usr/bin/env python3
"""Export Phase 10 step-2 answers.json template (② ask_with_candidate only)."""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from edit_proposal_schema import (  # noqa: E402
    VERDICT_ASK_WITH_CANDIDATE,
    align_proposal_spans_in_text,
    normalize_verdict,
)
from edit_proposal_schema import to_unknown_point  # noqa: E402
from question_bundle import bundle_safe_answer_items  # noqa: E402

JOB_GLOB_DEFAULT = "job_20260330_164142*"
INPUT_ROOT_DEFAULT = "data/transcriptions"


def _highlight_word(display: str, word: str) -> str:
    w = str(word or "").strip()
    if not w or w in display and "【" in display:
        return display
    if w and w in display:
        return display.replace(w, f"【{w}】", 1)
    return f"【{display}】" if display else f"【{w}】"


def _build_question_text(proposal: dict) -> str:
    """Hirose-style free_text question for hypothesis check."""
    span_before = str(proposal.get("span_before") or "").strip()
    hypothesis = str(proposal.get("hypothesis") or "").strip()
    anomaly_word = str(proposal.get("anomaly_word") or "").strip()
    display = span_before or anomaly_word
    display = _highlight_word(display, anomaly_word or span_before[:20])
    if hypothesis:
        return (
            f"「{display}」は「{hypothesis}」では？ "
            "合っていれば「正しい」、違えば正しい語、議事録に不要なら「削除」と返信してください。"
        )
    return (
        f"「{display}」はこの文脈に合いません。"
        "正しい語があれば返信、不要なら「削除」と返信してください。"
    )


def _sentences_around(
    text: str,
    pos: int,
    span_len: int,
    *,
    n_before: int = 2,
    n_after: int = 1,
) -> str:
    parts: list[tuple[int, int, str]] = []
    start = 0
    for m in re.finditer(r"[。!?]", text):
        end = m.end()
        chunk = text[start:end].strip()
        if chunk:
            parts.append((start, end, chunk))
        start = end
    if start < len(text):
        tail = text[start:].strip()
        if tail:
            parts.append((start, len(text), tail))
    if not parts:
        return text[max(0, pos - 200) : pos + span_len + 200].strip()

    center = pos + max(span_len // 2, 0)
    idx = 0
    for i, (s, e, _) in enumerate(parts):
        if s <= center < e:
            idx = i
            break
    else:
        idx = max(0, len(parts) - 1)

    lo = max(0, idx - n_before)
    hi = min(len(parts), idx + n_after + 1)
    return "\n\n".join(parts[j][2] for j in range(lo, hi))


def _context_for_proposal(proposal: dict, text: str) -> str:
    span_before = str(proposal.get("span_before") or "")
    start = int(proposal.get("span_start") or -1)
    if start < 0 and span_before:
        start = text.find(span_before)
    if start >= 0 and span_before:
        return _sentences_around(text, start, len(span_before))
    evidence = str(proposal.get("evidence") or "").strip()
    if evidence:
        return evidence
    return str(proposal.get("reason") or "").strip()


def export_answers_template(
    job_dir: Path,
    *,
    verdict_filter: str = VERDICT_ASK_WITH_CANDIDATE,
) -> tuple[list[dict], dict]:
    mech_path = job_dir / "merged_transcript_mechanical.txt"
    props_path = job_dir / "edit_proposals.json"
    if not mech_path.is_file():
        raise FileNotFoundError(f"missing {mech_path}")
    if not props_path.is_file():
        raise FileNotFoundError(f"missing {props_path}")

    text = mech_path.read_text(encoding="utf-8")
    doc = json.loads(props_path.read_text(encoding="utf-8"))
    proposals = list(doc.get("proposals") or [])

    selected: list[dict] = []
    for raw in proposals:
        if normalize_verdict(raw.get("verdict")) != verdict_filter:
            continue
        p = dict(raw)
        align_proposal_spans_in_text(text, p)
        selected.append(p)

    selected.sort(
        key=lambda p: (
            int(p.get("span_start") or -1),
            str(p.get("anomaly_word") or ""),
        )
    )

    answers: list[dict] = []
    for i, p in enumerate(selected, start=1):
        qid = str(uuid.uuid4())
        span_before = str(p.get("span_before") or "").strip()
        hypothesis = str(p.get("hypothesis") or "").strip()
        context = _context_for_proposal(p, text)
        question_text = _build_question_text(p)
        try:
            unknown_point = to_unknown_point(p)
        except ValueError:
            unknown_point = {}

        answers.append(
            {
                "question_id": qid,
                "question_text": question_text,
                "answer_text": "",
                "answered_at": None,
                "review_index": i,
                "proposal_id": p.get("proposal_id"),
                "anomaly_word": p.get("anomaly_word"),
                "span_before": span_before,
                "hypothesis": hypothesis,
                "context": context,
                "fact_class": p.get("fact_class"),
                "reason": p.get("reason"),
                "importance": p.get("importance"),
                "span_start": p.get("span_start"),
                "selected_unknown": unknown_point,
            }
        )

    bundled = bundle_safe_answer_items(answers)
    meta = {
        "job_id": job_dir.name,
        "pipeline_editor_version": doc.get("pipeline_editor_version"),
        "verdict_filter": verdict_filter,
        "count": len(bundled),
        "count_before_bundle": len(answers),
        "bundle_safe_enabled": True,
        "mechanical_chars": len(text),
    }
    return bundled, meta


def _write_markdown_review(path: Path, answers: list[dict], meta: dict) -> None:
    lines = [
        f"# Phase 10 Step 2 — ②候補確認 ({meta.get('job_id')})",
        "",
        f"- 件数: **{meta.get('count')}**（`ask_with_candidate` のみ）",
        f"- モード: **A** — `answer_text` に正解を書き込む（空欄のまま = 未回答）",
        f"- 回答例: `正しい` / 正しい語 / `削除`",
        "",
        "---",
        "",
    ]
    for item in answers:
        idx = item.get("review_index")
        lines.append(f"## {idx}. {item.get('anomaly_word') or '(no word)'}")
        lines.append("")
        lines.append(f"- **hypothesis（Opus候補）**: `{item.get('hypothesis')}`")
        lines.append(f"- **fact_class**: {item.get('fact_class')}")
        lines.append("")
        lines.append("### span_before")
        lines.append(f"```\n{item.get('span_before')}\n```")
        lines.append("")
        lines.append("### 周辺文脈（前後2〜3文）")
        lines.append(f"```\n{item.get('context')}\n```")
        lines.append("")
        lines.append("### question_text（answers.json と同一）")
        lines.append(f"> {item.get('question_text')}")
        lines.append("")
        lines.append(f"- `question_id`: `{item.get('question_id')}`")
        lines.append("")
        lines.append("---")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export ② answers.json template")
    parser.add_argument("--job-dir", help="Job directory path")
    parser.add_argument("--job-glob", default=JOB_GLOB_DEFAULT)
    parser.add_argument("--input-root", default=INPUT_ROOT_DEFAULT)
    parser.add_argument(
        "--fixture-out",
        default="scripts/fixtures/phase10_2_4_164142_answers_template.json",
    )
    parser.add_argument(
        "--review-md-out",
        default="scripts/fixtures/phase10_2_4_164142_answers_review.md",
    )
    args = parser.parse_args()

    if args.job_dir:
        job_dir = Path(args.job_dir)
    else:
        job_dir = next((ROOT / args.input_root).glob(args.job_glob))

    answers, meta = export_answers_template(job_dir)
    out_job = job_dir / "answers.json"
    out_job.write_text(json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8")

    fixture_path = ROOT / args.fixture_out
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_payload = {"meta": meta, "answers": answers}
    fixture_path.write_text(
        json.dumps(fixture_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    review_path = ROOT / args.review_md_out
    _write_markdown_review(review_path, answers, meta)

    print(json.dumps({**meta, "answers_path": str(out_job), "fixture_path": str(fixture_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
