#!/usr/bin/env python3
"""NREPT（國井様・村上様・山田様）ジョブ: 確定誤変換を反映し発言録・議事録・Doc を再出力する。

確定内容（ユーザー確認済み 2026-07-09）:
- 8年時 → 8年次（7年次と対になる年次表記）
- 期間色 → 基幹職（L1終了後の職階文脈）
- 示唆 → 指示（課長・先輩が部下に仕事をさせる文脈）
- 2点合えば → 日程合えば（トライアル案内への返答）
- 刺そう → 差そう（空雨傘の比喩）
- 相槌織り込みの除去（はいで/はいに 等、文脈付き）
- 言いかけ残骸「します。」の除去（村上先生紹介）

1. 学習辞書 scope=context へ記録
2. after_qa へピンポイント置換
3. reprocess --from-step resume（整文 + 最終批評 apply 含む）
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

JOB_ID = "job_20260709_025405_2026_0709_NREPT_國井様_村上様_山田様_相原"
INPUT_ROOT = "data/transcriptions"

CONTEXT_LEARNINGS: list[tuple[str, str, str]] = [
    ("8年時", "8年次", "当社の場合って8年時を対象に問題解決やってる"),
    ("期間色", "基幹職", "L1が終わると期間色というか、マネジメント色の方に"),
    ("示唆", "指示", "先輩とか課長とか、なんかそういう示唆で仕事をしなければ"),
    ("2点合えば", "日程合えば", "ぜひ！2点合えばちょっと見てみたいなと"),
]

MANUAL_FIXES: list[tuple[str, str]] = [
    ("8年時", "8年次"),
    ("期間色というか", "基幹職というか"),
    ("そういう示唆で仕事を", "そういう指示で仕事を"),
    ("2点合えば", "日程合えば"),
    ("傘を刺そう", "傘を差そう"),
    ("村上先生、一言お願いします。します。", "村上先生、一言お願いします。"),
    ("A1が4クラス80名、はいで、L2が", "A1が4クラス80名、L2が"),
    ("問題解決研修、はいで当社として", "問題解決研修、当社として"),
    ("実施をしない方向、はいに考えています", "実施をしない方向に考えています"),
    ("はいで、内容がL、今うちで", "で、内容がL、今うちで"),
    ("はいで、対象がL1の昇格", "対象がL1の昇格"),
    ("はいで、それで相原さんに", "それで相原さんに"),
]


def _py() -> str:
    return sys.executable


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    print(f"run: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(REPO), env=env or os.environ.copy()).returncode


def _record_learnings() -> None:
    for wrong, right, example in CONTEXT_LEARNINGS:
        r = add_learned_correction(
            wrong=wrong,
            right=right,
            via="chat_fix",
            job_id=JOB_ID,
            example=example,
            confidence="high",
            scope="context",
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
    # reprocess 時の整文パスで最終批評 apply を有効化（Railway env と揃える）
    env.setdefault("FINAL_REVIEW_MODE", "apply")

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
            "nrept_reentry_fixes",
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
    print("nrept_reentry_fixes_done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
