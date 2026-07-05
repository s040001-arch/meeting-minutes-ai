#!/usr/bin/env python3
"""thrジョブ P3: 確定誤変換の本文修正（混入回答文の除去含む）→ resume →
不明分の LINE バッチ質問送信までを一括実行する。

手順:
  1. MANUAL_FIXES を after_qa に適用（混入回答文2箇所の復元を含む）
  2. reprocess --from-step resume で議事録再生成 + Doc 反映
  3. reenter_completed_job で不明分7件を注入し LINE バッチ質問を送信
     （ジョブは再び回答待ち。回答後は run_answer_light の通常経路で完了）

使い方:
  python scripts/apply_thr_reentry_fixes.py [--skip-questions] [--dry-run]
"""
from __future__ import annotations

import argparse
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
ITEMS = "scripts/fixtures/thr_reentry_items.json"

# 混入回答文の除去（P1事象の実データ復旧）。位置は 2026-07-05 の実測値。
INJECTED_ANSWER_FIXES: list[tuple[str, str]] = [
    (
        "この辺りに1番は「西脇さん」で正しい。2番は「西脇さん」ではなく「相原」。とかどんな感じられます",
        "この辺りに西脇さんとかどんな感じられます",
    ),
    (
        "ま、1番は「西脇さん」で正しい。2番は「西脇さん」ではなく「相原」。がおっしゃっている",
        "ま、相原がおっしゃっている",
    ),
]

# 確定扱いの誤変換修正（文脈を含めて特定できる形で置換）
MANUAL_FIXES: list[tuple[str, str]] = [
    # 給食 → 休職（不在・復帰文脈）
    ("あの給食に入ってる", "あの休職に入ってる"),
    # 個数 → 工数（作業量文脈。この会議では全出現が工数）
    ("個数が膨らんでしまってる", "工数が膨らんでしまってる"),
    ("個数を減らすことができると良いな", "工数を減らすことができると良いな"),
    ("個数としてまたかかってない", "工数としてまたかかってない"),
    ("事務局の個数の削減", "事務局の工数の削減"),
    # 回収 → 改修（システム・セッション文脈のみ）
    ("システム上、ちょっと回収が必要", "システム上、ちょっと改修が必要"),
    ("システムの回収", "システムの改修"),
    ("あの回収が済んで", "あの改修が済んで"),
    ("セッション回収", "セッション改修"),
    # 転機する・展記・天気して・転勤をした → 転記
    ("それを転機するっていう作業", "それを転記するっていう作業"),
    ("ちゃんと展記されるような仕組み", "ちゃんと転記されるような仕組み"),
    ("間違いなく転機されるような仕組み", "間違いなく転記されるような仕組み"),
    ("っていうことを天気して", "っていうことを転記して"),
    ("その転勤をしたことに対して", "その転記をしたことに対して"),
    # 配信できる → 廃止できる（ドネ協定の確認を無くせる文脈のみ）
    ("確認っていうのが配信できる", "確認っていうのが廃止できる"),
    # 生活確認書 → 日程確認書
    ("生活確認書", "日程確認書"),
    # ギター浅井さん → 確か浅井さんにおかれましては
    ("ギター浅井さんに置かれましては", "確か浅井さんにおかれましては"),
]


def _py() -> str:
    return sys.executable


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    print(f"run: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(REPO), env=env or os.environ.copy()).returncode


def _apply_fixes(job_dir: Path, *, dry_run: bool) -> int:
    path = job_dir / "merged_transcript_after_qa.txt"
    if not path.is_file():
        print(f"after_qa_missing={path}")
        return -1
    text = path.read_text(encoding="utf-8")
    n = 0
    for old, new in INJECTED_ANSWER_FIXES + MANUAL_FIXES:
        c = text.count(old)
        if c > 0:
            text = text.replace(old, new)
            n += c
            print(f"fix x{c}: {old!r} -> {new!r}")
        else:
            print(f"not_found: {old!r}")
    if n and not dry_run:
        path.write_text(text, encoding="utf-8")
        print(f"after_qa_saved fixes={n}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-questions", action="store_true", help="LINE質問の再送をしない")
    ap.add_argument("--dry-run", action="store_true", help="置換プレビューのみ")
    args = ap.parse_args()

    load_dotenv_local()
    job_dir = REPO / INPUT_ROOT / JOB_ID
    if not job_dir.is_dir():
        print(f"job_dir_missing={job_dir}")
        return 1

    # 1) 確定分の本文修正
    fixed = _apply_fixes(job_dir, dry_run=args.dry_run)
    if fixed < 0:
        return 1
    if args.dry_run:
        print("[dry-run] resume・質問送信は行いません。")
        return 0

    env = os.environ.copy()
    env["QUESTION_MODE"] = env.get("QUESTION_MODE") or "line"

    # 2) 議事録再生成 + Doc 反映
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
            "thr_reentry_fixes",
        ],
        env=env,
    )
    if rc != 0:
        return rc

    if args.skip_questions:
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
            detail={"reason": "thr_reentry_fixes_no_questions"},
            overall_status="success",
        )
        print("thr_reentry_fixes_done (questions skipped)")
        return 0

    # 3) 不明分を注入して LINE バッチ質問（ジョブは回答待ちに戻る）
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
            "--send-line",
        ],
        env=env,
    )
    if rc != 0:
        return rc
    print("thr_reentry_fixes_done (awaiting LINE answers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
