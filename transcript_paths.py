"""ジョブディレクトリ内の代表トランスクリプトパスを解決する共通ヘルパー。"""

import os

# LINE 回答反映後も逐語録の大部分を維持しているかの目安（これ未満なら ai を優先）
MIN_TRANSCRIPT_LENGTH_RATIO = 0.85


def _read_text_length(path: str) -> int:
    if not os.path.isfile(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return len(f.read().strip())


def resolve_transcript_path(job_id: str, input_path: str | None, input_root: str) -> str:
    if input_path:
        return input_path
    job_dir = os.path.join(input_root, job_id)
    for name in ("merged_transcript_ai.txt", "merged_transcript.txt"):
        p = os.path.join(job_dir, name)
        if os.path.isfile(p):
            return p
    return os.path.join(job_dir, "merged_transcript_ai.txt")


def resolve_transcript_path_for_minutes(
    job_id: str,
    input_path: str | None,
    input_root: str,
) -> str:
    """議事録生成用: after_qa が異常に短い場合は merged_transcript_ai を優先する。"""
    if input_path:
        return input_path
    job_dir = os.path.join(input_root, job_id)
    ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
    after_qa_path = os.path.join(job_dir, "merged_transcript_after_qa.txt")
    merged_path = os.path.join(job_dir, "merged_transcript.txt")

    ai_len = _read_text_length(ai_path)
    after_len = _read_text_length(after_qa_path)
    if after_len > 0 and ai_len > 0 and after_len >= ai_len * MIN_TRANSCRIPT_LENGTH_RATIO:
        return after_qa_path
    if ai_len > 0:
        return ai_path
    if after_len > 0:
        return after_qa_path
    if os.path.isfile(merged_path):
        return merged_path
    return after_qa_path
