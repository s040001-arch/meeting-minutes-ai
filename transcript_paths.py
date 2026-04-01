"""ジョブディレクトリ内の代表トランスクリプトパスを解決する共通ヘルパー。"""

import os


def resolve_transcript_path(job_id: str, input_path: str | None, input_root: str) -> str:
    if input_path:
        return input_path
    job_dir = os.path.join(input_root, job_id)
    for name in ("merged_transcript_ai.txt", "merged_transcript.txt"):
        p = os.path.join(job_dir, name)
        if os.path.isfile(p):
            return p
    return os.path.join(job_dir, "merged_transcript_ai.txt")
