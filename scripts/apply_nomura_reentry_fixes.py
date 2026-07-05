#!/usr/bin/env python3
"""野村ジョブ: 検出漏れ誤変換を注入し、既知回答で反映→議事録再生成まで一括実行。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from progress_tracker import update_job_progress
from question_mode import clear_pause_marker
from repo_env import load_dotenv_local

JOB_ID = (
    "job_20260704_120524_2026_0629_野村不動産社_物流事業__半田様_川口様_徳重_相原"
)
INPUT_ROOT = "data/transcriptions"
ITEMS = "scripts/fixtures/nomura_reentry_items.json"
ANSWERS_JSON = os.path.join("data", "line_answers.json")
REPO = Path(__file__).resolve().parents[1]

# 単語置換では直せない分割崩れ・残留誤変換（recorrect 後に適用）
MANUAL_FIXES: list[tuple[str, str]] = [
    ("あやさん", "相原さん"),
    ("ボールを前描いた", "ゴールを前に置いた"),
    ("お客さん思考", "逆算思考"),
    ("決済", "決裁"),
    ("冠水", "感覚"),
    ("実務が阻害要因になっているので、", ""),
    ("それをリンクとか、", "それを"),
    ("統計の方", "同期の方"),
]

_KNOWN_OK_WORDS = frozenset(
    {"統計", "あやさん", "ボール", "お客さん思考", "決済", "冠水"}
)


def _py() -> str:
    return sys.executable


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    print(f"run: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(REPO), env=env or os.environ.copy()).returncode


def _build_batch_answer(question_result: dict) -> str:
    items = (question_result.get("selected_unknown") or {}).get("batch_items") or []
    parts: list[str] = []
    for i, it in enumerate(items, 1):
        cand = str(it.get("estimated_correction") or "").strip()
        if cand:
            parts.append(f"{i} {cand}")
        elif str(it.get("word") or "").strip() in _KNOWN_OK_WORDS:
            parts.append(f"{i} OK")
        else:
            parts.append(f"{i} 不明")
    return " / ".join(parts)


def _save_answer_record(
    *,
    job_dir: Path,
    question_id: str,
    question_text: str,
    answer_text: str,
) -> str:
    """ジョブ answers.json に追記（recorrect の優先読み込み先）。"""
    path = job_dir / "answers.json"
    existing: list = []
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []
    if not isinstance(existing, list):
        existing = []
    existing.append(
        {
            "received_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
            "question_id": question_id,
            "question_text": question_text,
            "answer_text": answer_text,
            "user_id": "apply_nomura_reentry_fixes",
            "job_id": JOB_ID,
        }
    )
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"answer_saved path={path} question_id={question_id} text={answer_text!r}")
    return str(path)


def _apply_manual_fixes(job_dir: Path) -> int:
    path = job_dir / "merged_transcript_after_qa.txt"
    if not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8")
    n = 0
    for old, new in MANUAL_FIXES:
        if old in text:
            text = text.replace(old, new)
            n += 1
            print(f"manual_fix: {old!r} -> {new!r}")
    if n:
        path.write_text(text, encoding="utf-8")
    return n


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--direct-only",
        action="store_true",
        help="逐語録の手動置換と resume のみ（再注入・recorrect しない）",
    )
    args = ap.parse_args()

    load_dotenv_local()
    job_dir = REPO / INPUT_ROOT / JOB_ID
    if not job_dir.is_dir():
        print(f"job_dir_missing={job_dir}")
        return 1

    env = os.environ.copy()
    env["QUESTION_MODE"] = env.get("QUESTION_MODE") or "line"

    if not args.direct_only:
        # 1) 注入 + 質問生成（LINE送信なし）
        rc = _run(
            [
                _py(),
                str(REPO / "reenter_completed_job.py"),
                "--job-id",
                JOB_ID,
                "--input-root",
                INPUT_ROOT,
                "--items",
                ITEMS,
            ],
            env=env,
        )
        if rc != 0:
            return rc

        qr_path = job_dir / "question_result.json"
        if not qr_path.is_file():
            print("question_result.json missing after reenter")
            return 1
        question_result = json.loads(qr_path.read_text(encoding="utf-8"))
        question_id = str(question_result.get("question_id") or "").strip()
        if not question_id:
            print("question_id missing")
            return 1

        answer_text = _build_batch_answer(question_result)
        answers_path = _save_answer_record(
            job_dir=job_dir,
            question_id=question_id,
            question_text=str(question_result.get("question_text") or ""),
            answer_text=answer_text,
        )

        # 2) 回答反映
        rc = _run(
            [
                _py(),
                str(REPO / "recorrect_from_line_answer.py"),
                "--job-id",
                JOB_ID,
                "--input-root",
                INPUT_ROOT,
                "--answers-json",
                answers_path,
                "--question-id",
                question_id,
            ],
            env=env,
        )
        if rc != 0:
            return rc

    # 3) 手動スパン修正
    _apply_manual_fixes(job_dir)

    # 4) 議事録再生成 + Doc反映
    rc = _run(
        [
            _py(),
            str(REPO / "reprocess_job.py"),
            "--job-dir",
            str(job_dir),
            "--input-root",
            INPUT_ROOT,
            "--from-step",
            "resume",
            "--reason",
            "nomura_reentry_fixes",
        ],
        env=env,
    )
    if rc != 0:
        return rc

    clear_pause_marker(str(job_dir))
    from run_answer_light import _mark_doc_completed, send_completion_line

    _mark_doc_completed(str(job_dir), JOB_ID, INPUT_ROOT, "")
    send_line = bool(
        os.getenv("LINE_USER_ID", "").strip()
        and os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    )
    completion = send_completion_line(
        str(job_dir), job_id=JOB_ID, send_line=send_line
    )
    print(f"completion_line={completion}")

    update_job_progress(
        input_root=INPUT_ROOT,
        job_id=JOB_ID,
        phase="done",
        status="success",
        detail={"reason": "nomura_reentry_fixes"},
        overall_status="success",
    )
    print("nomura_reentry_fixes_done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
