#!/usr/bin/env python3
"""デイシス社（高田様）ジョブ: 確定誤変換を反映し発言録・議事録・Doc を再出力する。

確定内容（ユーザー確認済み 2026-07-08）:
- 藤井さん → 富士山（correction_dict 汚染の復元。地名文脈）
- テニス / レース → デイシス（会社名。2003年中途入社の文脈）
- 宮崎 → 矢崎（顧客企業名。期初 7/21 の文脈）
- 自立的 → 自律的
- 車内 → 社内（社内共有・社内流通の文脈。車載メーターの「車載」は残す）

1. 文脈確定ペアを学習辞書 scope=context へ記録（次ジョブから検出ヒント）
2. after_qa へピンポイント置換（前後文脈つきで誤爆防止）
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

from learned_corrections_store import add_learned_correction  # noqa: E402
from question_mode import clear_pause_marker  # noqa: E402
from repo_env import load_dotenv_local  # noqa: E402

JOB_ID = "job_20260708_063801_2026_0708_デイシス社_高田様_松本_相原"
INPUT_ROOT = "data/transcriptions"

# 実在語ペア → 検出ヒントとして学習（盲目置換しない）
CONTEXT_LEARNINGS: list[tuple[str, str, str]] = [
    ("テニス", "デイシス", "テニス自体は 2003 年に中途で入ってます"),
    ("宮崎", "矢崎", "今宮崎が 7 月の 21 日から新しい期になって"),
    ("自立的", "自律的", "自立的な判断とか"),
]

# 前後文脈つきピンポイント置換（この job の after_qa にのみ適用）
MANUAL_FIXES: list[tuple[str, str]] = [
    ("藤井さんの麓", "富士山の麓"),
    ("藤井さんの山口宮市", "富士山の富士宮市"),
    ("藤井さん所有市", "富士山所有市"),
    ("テニス自体は 2003 年に", "デイシス自体は 2003 年に"),
    ("レース入ってるんですけど", "デイシスに入ってるんですけど"),
    ("今宮崎が 7 月の 21 日から", "今矢崎が 7 月の 21 日から"),
    ("自立的な判断", "自律的な判断"),
    ("車内でもそこで", "社内でもそこで"),
    ("車内で伝えてるような情報", "社内で伝えてるような情報"),
    ("車内で流通させて", "社内で流通させて"),
]


def _py() -> str:
    return sys.executable


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    print(f"run: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(REPO), env=env or os.environ.copy()).returncode


def _record_learnings() -> None:
    for wrong, right, example in CONTEXT_LEARNINGS:
        r = add_learned_correction(
            wrong=wrong, right=right, via="chat_fix", job_id=JOB_ID,
            example=example, confidence="high", scope="context",
        )
        print(f"learned[context]: {wrong!r} -> {right!r} ({r.get('action')})")


def _apply_fixes(job_dir: Path) -> int:
    path = job_dir / "merged_transcript_after_qa.txt"
    if not path.is_file():
        print(f"after_qa_missing={path}")
        return 0
    text = path.read_text(encoding="utf-8")
    before = text
    n = 0
    for old, new in MANUAL_FIXES:
        if old in text:
            text = text.replace(old, new)
            n += 1
            print(f"manual_fix: {old!r} -> {new!r}")
        else:
            print(f"manual_fix_not_found: {old!r}")
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

    _record_learnings()
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
            "daisys_reentry_fixes",
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
    completion = send_completion_line(str(job_dir), job_id=JOB_ID, send_line=send_line)
    print(f"completion_line={completion}")
    print("daisys_reentry_fixes_done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
