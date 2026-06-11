"""既存ジョブで Step 4.5（整合性レビュー）以降を再実行する。

デプロイ後の品質改善を、音声の再投入なしで反映するための入口。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from unknown_point_filters import filter_answerable_unknown_points

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _py() -> str:
    return sys.executable


def _run(cmd: list[str], step: str) -> None:
    print(f"[run_resume_from_coherence] {step}: {' '.join(cmd)}", flush=True)
    completed = subprocess.run(cmd, cwd=REPO_ROOT)
    if completed.returncode != 0:
        raise RuntimeError(f"{step} failed: exit_code={completed.returncode}")


def _load_unknowns(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _save_unknowns(path: str, items: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _is_coherence(item: dict) -> bool:
    return (
        str(item.get("source") or "") == "coherence_review"
        or str(item.get("type") or "") == "coherence_review"
    )


def _refresh_unknown_points(unknowns_path: str) -> None:
    """coherence 項目を除去し、detect 由来の非回答可能論点を再フィルタする。"""
    items = _load_unknowns(unknowns_path)
    without_coherence = [x for x in items if not _is_coherence(x)]
    filtered, dropped = filter_answerable_unknown_points(without_coherence)
    _save_unknowns(unknowns_path, filtered)
    print(
        f"unknown_points_refreshed coherence_removed={len(items) - len(without_coherence)} "
        f"non_answerable_dropped={dropped} remaining={len(filtered)}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 4.5 coherence 以降（質問選定・議事録・Docs）を再実行"
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument(
        "--send-line",
        action="store_true",
        help="質問が生成された場合 LINE push する",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Google Docs を更新",
    )
    parser.add_argument(
        "--min-question-value",
        type=int,
        default=7,
        help="detect 由来質問の proposal_impact 閾値（coherence は常に優先）",
    )
    args = parser.parse_args()

    job_dir = os.path.join(args.input_root, args.job_id)
    if not os.path.isdir(job_dir):
        raise FileNotFoundError(f"job dir not found: {job_dir}")
    ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
    if not os.path.isfile(ai_path):
        raise FileNotFoundError(f"merged_transcript_ai.txt not found: {ai_path}")

    unknowns_path = os.path.join(job_dir, "unknown_points.json")
    _refresh_unknown_points(unknowns_path)

    _run(
        [
            _py(),
            os.path.join(REPO_ROOT, "coherence_review.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
        ],
        "coherence_review",
    )

    qcycle_cmd = [
        _py(),
        os.path.join(REPO_ROOT, "run_question_cycle_once.py"),
        "--job-id",
        args.job_id,
        "--input-root",
        args.input_root,
        "--unknowns",
        unknowns_path,
        "--text",
        ai_path,
        "--min-question-value",
        str(args.min_question_value),
    ]
    if args.send_line:
        qcycle_cmd.append("--send-line")
    _run(qcycle_cmd, "run_question_cycle_once")

    after_qa = os.path.join(job_dir, "merged_transcript_after_qa.txt")
    if not os.path.isfile(after_qa):
        import shutil

        shutil.copy2(ai_path, after_qa)

    _run(
        [
            _py(),
            os.path.join(REPO_ROOT, "generate_minutes_transcript.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
        ],
        "generate_minutes_transcript",
    )
    _run(
        [
            _py(),
            os.path.join(REPO_ROOT, "generate_minutes_other_sections.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
        ],
        "generate_minutes_other_sections",
    )

    hub_cmd = [
        _py(),
        os.path.join(REPO_ROOT, "run_docs_hub_e2e.py"),
        "--job-id",
        args.job_id,
        "--input-root",
        args.input_root,
        "--skip-compose",
    ]
    if args.push:
        hub_cmd.append("--push")
    _run(hub_cmd, "run_docs_hub_e2e")

    print(f"run_resume_from_coherence_done job_id={args.job_id}")


if __name__ == "__main__":
    main()
