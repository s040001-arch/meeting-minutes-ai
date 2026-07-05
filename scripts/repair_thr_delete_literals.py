#!/usr/bin/env python3
"""thrジョブ: バッチ回答の delete 誤適用で混入した「削除」literal を修復し resume する。

原因: 「1=削除」形式がパーサ未対応で correction=削除 として literal 置換された。
修復後は recognition_batch の =区切り・delete 対応済みコードで再発しない。
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

from repo_env import load_dotenv_local  # noqa: E402

JOB_ID = "job_20260705_055804_2026_0624_thr社_運営改善_西脇様_竹中様_福田_相原"
INPUT_ROOT = "data/transcriptions"

# バッチ delete 誤適用で literal「削除」になった4箇所 + 愛ナ→相原の座り直し
LITERAL_DELETE_FIXES: list[tuple[str, str]] = [
    ("あの削除等々", "あの等々"),
    ("削除サポート", "サポート"),
    ("誰から見ても削除で", "誰から見ても、どのように"),
    ("の県の本当に削除いこうか", "の件は本当に共有していこうか"),
    ("先日さんの相原さんに", "先日相原さんに"),
]


def _py() -> str:
    return sys.executable


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    print(f"run: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(REPO), env=env or os.environ.copy()).returncode


def main() -> int:
    load_dotenv_local()
    job_dir = REPO / INPUT_ROOT / JOB_ID
    path = job_dir / "merged_transcript_after_qa.txt"
    if not path.is_file():
        print(f"missing={path}")
        return 1

    text = path.read_text(encoding="utf-8")
    n = 0
    for old, new in LITERAL_DELETE_FIXES:
        c = text.count(old)
        if c:
            text = text.replace(old, new)
            n += c
            print(f"fix x{c}: {old!r} -> {new!r}")
        else:
            print(f"not_found: {old!r}")
    if n == 0:
        print("no literal delete fixes applied")
        return 1
    path.write_text(text, encoding="utf-8")
    print(f"after_qa_saved fixes={n}")

    env = os.environ.copy()
    env["QUESTION_MODE"] = env.get("QUESTION_MODE") or "line"
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
            "thr_delete_literal_repair",
        ],
        env=env,
    )
    if rc != 0:
        return rc

    from question_mode import clear_pause_marker
    from progress_tracker import update_job_progress
    from run_answer_light import _mark_doc_completed, send_completion_line

    clear_pause_marker(str(job_dir))
    _mark_doc_completed(str(job_dir), JOB_ID, INPUT_ROOT, "")
    send_line = bool(
        os.getenv("LINE_USER_ID", "").strip()
        and os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    )
    completion = send_completion_line(str(job_dir), job_id=JOB_ID, send_line=send_line)
    print(f"completion_line={completion}")
    update_job_progress(
        input_root=INPUT_ROOT,
        job_id=JOB_ID,
        phase="done",
        status="success",
        detail={"reason": "thr_delete_literal_repair"},
        overall_status="success",
    )
    print("thr_delete_literal_repair_done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
