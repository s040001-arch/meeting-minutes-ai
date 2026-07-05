"""完了済みジョブに未検出の誤変換を注入し、バッチ確認質問を再送する。

すでに overall_status=success で終わったジョブでも、
検出漏れした誤変換（とくに 2 字語）を open な unknown_points として
逐語録上の実位置に紐づけて登録し、pause を張り直して再質問できるようにする。

注入項目は逐語録(after_qa)上で位置特定できたものだけ登録する
（Doc 表記ではなく逐語録表記を基準にする）。位置特定できない語は
陳腐化・誤爆を避けるためスキップする。

使い方:
  python reenter_completed_job.py \
      --job-id <JOB_ID> --input-root data/transcriptions \
      --items items.json [--send-line] [--dry-run]

items.json 例:
  [
    {"word": "決済", "correction": "決裁", "hint_pos": 13505},
    {"word": "あやさん", "correction": "相原さん"},
    {"word": "暑さ", "correction": ""}
  ]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from recognition_batch import (
    COHERENCE_SOURCE,
    COHERENCE_TYPE,
    build_batch_items,
    build_batch_question_text,
    find_standalone_word,
    is_valid_coherence_question_word,
)

UNKNOWN_POINTS_FILENAME = "unknown_points.json"
_TRANSCRIPT_CANDIDATES = (
    "merged_transcript_after_qa.txt",
    "merged_transcript_ai.txt",
    "merged_transcript.txt",
)
REENTRY_SOURCE_TAG = "manual_reentry"


def _load_transcript(job_dir: Path) -> tuple[str, str]:
    for name in _TRANSCRIPT_CANDIDATES:
        p = job_dir / name
        if p.is_file():
            return p.read_text(encoding="utf-8"), str(p)
    return "", ""


def _stable_anomaly_id(word: str, pos: int) -> str:
    h = hashlib.sha1(f"{REENTRY_SOURCE_TAG}:{word}:{pos}".encode("utf-8")).hexdigest()
    return f"reentry_{h[:12]}"


def _snippet(text: str, idx: int, word: str, half: int = 60) -> str:
    start = max(0, idx - half)
    end = min(len(text), idx + len(word) + half)
    return text[start:end].strip()


def _build_injected_span_hypothesis(text: str, spec: dict) -> dict | None:
    """崩壊文スパン＋復元仮説の注入項目を作る（文意確認モード）。

    spec 例:
      {"span": "ドネと協定を狩ることは廃止できる",
       "hypothesis": "NEST上の協定の確認は廃止できる",
       "reason": "Box節の文崩壊"}
    span が逐語録に一致しない場合はスキップ（陳腐化防止）。
    """
    span = str(spec.get("span") or "").strip()
    if not span:
        return None
    hypothesis = str(spec.get("hypothesis") or "").strip()
    pos = text.find(span)
    if pos < 0:
        print(f"  SKIP (逐語録にスパンが見つからない): {span[:40]!r}…")
        return None
    conf = str(spec.get("confidence") or "medium").strip().lower()
    return {
        "type": COHERENCE_TYPE,
        "source": COHERENCE_SOURCE,
        "reentry_source": REENTRY_SOURCE_TAG,
        "anomaly_id": _stable_anomaly_id(span[:40], pos),
        "anomaly_word": span,
        "text": span[:220],
        "context": span[:220],
        "span_text": span,
        "estimated_correction": hypothesis,
        "span_corrected": hypothesis,
        "confidence": conf if conf in {"high", "medium", "low"} else "medium",
        "anomaly_type": "C",
        "question_kind": "span_hypothesis",
        "reason": str(spec.get("reason") or "manual re-entry (文意確認)"),
        "context_position_in_transcript": pos,
        "force_question": True,
        "status": "open",
    }


def _build_injected_point(text: str, spec: dict) -> dict | None:
    if spec.get("span"):
        return _build_injected_span_hypothesis(text, spec)
    word = str(spec.get("word") or "").strip()
    if not word:
        return None
    correction = str(spec.get("correction") or "").strip()
    try:
        hint = int(spec.get("hint_pos", -1))
    except (TypeError, ValueError):
        hint = -1
    pos = find_standalone_word(text, word, hint_pos=hint)
    if pos < 0 and hint >= 0 and hint + len(word) <= len(text):
        if text[hint : hint + len(word)] == word:
            pos = hint
    if pos < 0:
        print(f"  SKIP (逐語録に見つからない): {word!r}")
        return None
    # force=true は人手キュレーション済みの証。位置特定できていればゲートを免除する
    # （候補なし2字の氏名など、自動検出では拾えないが人が確認したい語を許可）。
    force = bool(spec.get("force"))
    if not force and not is_valid_coherence_question_word(
        word, has_candidate=bool(correction), located=True
    ):
        print(f"  SKIP (語長ゲート: 候補なし2字等。force で明示注入可): {word!r}")
        return None
    span = _snippet(text, pos, word)
    conf = str(spec.get("confidence") or "medium").strip().lower()
    return {
        "type": COHERENCE_TYPE,
        "source": COHERENCE_SOURCE,
        "reentry_source": REENTRY_SOURCE_TAG,
        "anomaly_id": _stable_anomaly_id(word, pos),
        "anomaly_word": word,
        "text": span[:220],
        "context": span[:220],
        "span_text": span,
        "estimated_correction": correction,
        "span_corrected": "",
        "confidence": conf if conf in {"high", "medium", "low"} else "medium",
        "anomaly_type": "B",
        "reason": "manual re-entry (検出漏れ補完)",
        "context_position_in_transcript": pos,
        "force_question": force,
        "status": "open",
    }


def _merge_unknowns(path: Path, new_points: list[dict]) -> tuple[int, int]:
    existing: list[dict] = []
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing = []
    have_ids = {
        str(p.get("anomaly_id"))
        for p in existing
        if isinstance(p, dict) and p.get("anomaly_id")
    }
    added = 0
    for p in new_points:
        if p.get("anomaly_id") in have_ids:
            print(f"  既存(skip): {p.get('anomaly_word')} [{p.get('anomaly_id')}]")
            continue
        existing.append(p)
        have_ids.add(str(p.get("anomaly_id")))
        added += 1
    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return added, len(existing)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--input-root", default="data/transcriptions")
    ap.add_argument("--items", required=True, help="注入する誤変換 spec の JSON パス")
    ap.add_argument("--send-line", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="注入せずプレビューのみ")
    args = ap.parse_args()

    job_dir = Path(args.input_root) / args.job_id
    if not job_dir.is_dir():
        print(f"job_dir_missing={job_dir}")
        return 1

    text, text_path = _load_transcript(job_dir)
    if not text:
        print("transcript_missing")
        return 1
    print(f"transcript={text_path} (len={len(text)})")

    specs = json.loads(Path(args.items).read_text(encoding="utf-8"))
    if not isinstance(specs, list):
        print("items must be a JSON list")
        return 1

    injected: list[dict] = []
    print("=== 位置特定・ゲート判定 ===")
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        point = _build_injected_point(text, spec)
        if point:
            print(
                f"  OK: {point['anomaly_word']!r} @ "
                f"{point['context_position_in_transcript']} "
                f"→ {point['estimated_correction'] or '(候補なし)'}"
            )
            injected.append(point)

    if not injected:
        print("注入対象なし（位置特定できた項目が0件）")
        return 2

    # 現在の逐語録に対してバッチ質問プレビュー
    items = build_batch_items(injected, full_text=text)
    print(f"\n=== バッチ質問プレビュー（{len(items)}/{len(injected)}件が質問化）===")
    print(build_batch_question_text(items))

    if args.dry_run:
        print("\n[dry-run] 注入・pause・送信は行いませんでした。")
        return 0

    unknowns_path = job_dir / UNKNOWN_POINTS_FILENAME
    added, total = _merge_unknowns(unknowns_path, injected)
    print(f"\nunknown_points 追記: +{added} (total={total})")

    from question_mode import write_pause_marker
    from progress_tracker import update_job_progress

    write_pause_marker(
        job_dir,
        mode="line",
        question_artifacts=["unknown_points.json"],
        resume_hint=f"python reprocess_job.py --job-dir {job_dir} --from-step resume",
    )
    update_job_progress(
        input_root=args.input_root,
        job_id=args.job_id,
        phase="question_pause",
        status="success",
        detail={"reason": "reenter_completed_job", "injected": added},
        overall_status="paused",
    )
    print("pause 再設定 + overall_status=paused")

    cmd = [
        sys.executable,
        "run_question_cycle_once.py",
        "--job-id",
        args.job_id,
        "--input-root",
        args.input_root,
        "--unknowns",
        str(unknowns_path),
        "--text",
        text_path,
    ]
    if args.send_line:
        cmd.append("--send-line")
    env = os.environ.copy()
    env["QUESTION_MODE"] = env.get("QUESTION_MODE") or "line"
    cwd = "/app" if Path("/app").is_dir() else "."
    print(f"cmd={' '.join(cmd)}")
    rc = subprocess.run(cmd, cwd=cwd, env=env).returncode
    print(f"question_cycle_exit={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
