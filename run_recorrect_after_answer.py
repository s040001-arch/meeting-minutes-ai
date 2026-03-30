"""
LINE 回答後に、同一 job_id の再補正（recorrect_from_line_answer）だけを実行する最小入口。
議事録生成・Docs 出力は行わない。
"""

import argparse
import os
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "data/line_answers.json の回答を使い、recorrect_from_line_answer.py を1回実行する"
        )
    )
    parser.add_argument(
        "--job-id",
        required=True,
        help="対象ジョブID（recorrect_from_line_answer と同じ）",
    )
    args = parser.parse_args()

    repo_root = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(repo_root, "recorrect_from_line_answer.py")
    cmd = [sys.executable, target, "--job-id", args.job_id]
    subprocess.run(cmd, check=True, cwd=repo_root)


if __name__ == "__main__":
    main()
