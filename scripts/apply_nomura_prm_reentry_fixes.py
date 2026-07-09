#!/usr/bin/env python3
"""プレセナ提案レビュー会(物流事業部向けプロマネ研修)の確定誤変換を反映し発言録を再生成する。

1. 学習辞書の global ペアを after_qa に機械適用
2. 文脈確定の手動置換
3. reprocess --from-step resume で整文・議事録・Doc 再出力
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mechanical_correct_text import apply_mechanical_corrections  # noqa: E402
from question_mode import clear_pause_marker  # noqa: E402
from repo_env import load_dotenv_local  # noqa: E402

JOB_ID = (
    "job_20260708_043610_2026_0708_プレセナ社_提案レビュー会_"
    "物流事業部向けプロマネ研修_荻野"
)
INPUT_ROOT = "data/transcriptions"

# バッチ回答・会話で確定した文脈付き置換（盲目 global 化しないもの）
MANUAL_FIXES: list[tuple[str, str]] = [
    ("新卒ブロック", "新卒プロパー"),
    ("GPDC", "G-PDCA"),
    ("gpdc", "G-PDCA"),
    ("最小項数", "最小工数"),
    ("累計化がある気がして", "類型化がある気がして"),
    ("全部漏らされてる", "全部網羅されてる"),
    ("自分の裁きとして", "自分の裁量として"),
    ("富士山が以前構成", "藤井さんが以前構成"),
    ("プロ丸", "プロマネ"),
    # 稟議・承認プロセス文脈の決済（支払い決済は残す）
    ("決済するフェーズ", "決裁するフェーズ"),
    ("決済ルート", "決裁ルート"),
    ("決済してもらわなきゃ", "決裁してもらわなきゃ"),
    ("決済をちゃんと適切に", "決裁をちゃんと適切に"),
    ("部長レベルに決済", "部長レベルに決裁"),
    ("役員レベルや部長レベルに決済", "役員レベルや部長レベルに決裁"),
]


def _py() -> str:
    return sys.executable


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    print(f"run: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(REPO), env=env or os.environ.copy()).returncode


def _apply_fixes(job_dir: Path) -> int:
    path = job_dir / "merged_transcript_after_qa.txt"
    if not path.is_file():
        print(f"after_qa_missing={path}")
        return 0
    text = path.read_text(encoding="utf-8")
    before = text

    text = apply_mechanical_corrections(text, job_dir=str(job_dir))
    # global learned: 火球分析, 姿勢のに提供している 等

    n = 0
    for old, new in MANUAL_FIXES:
        if old in text:
            text = text.replace(old, new)
            n += 1
            print(f"manual_fix: {old!r} -> {new!r}")

    if text != before:
        path.write_text(text, encoding="utf-8")
        print(f"after_qa_saved manual_fixes={n} chars={len(text)}")
    else:
        print("after_qa_unchanged")
    return n


def main() -> int:
    load_dotenv_local()
    job_dir = REPO / INPUT_ROOT / JOB_ID
    if not job_dir.is_dir():
        print(f"job_dir_missing={job_dir}")
        return 1

    env = os.environ.copy()
    env["QUESTION_MODE"] = env.get("QUESTION_MODE") or "line"
    env.setdefault("READABLE_TRANSCRIPT_ENABLED", "1")

    _apply_fixes(job_dir)

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
            "nomura_prm_reentry_fixes",
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
    print("nomura_prm_reentry_fixes_done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
