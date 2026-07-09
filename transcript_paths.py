"""ジョブディレクトリ内の代表トランスクリプトパスを解決する共通ヘルパー。"""

import json
import os
import re

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


def drive_file_view_url(file_id: str) -> str:
    fid = str(file_id or "").strip()
    if not fid:
        return ""
    return f"https://drive.google.com/file/d/{fid}/view"


def _read_google_doc_hub(job_dir: str) -> dict:
    path = os.path.join(job_dir, "google_doc_hub.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _source_id_from_docs_write_log(job_dir: str) -> str:
    for name in ("docs_write_log.txt", os.path.join("drive", "docs_write_log.txt")):
        log_path = os.path.join(job_dir, name)
        if not os.path.isfile(log_path):
            continue
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"^uploaded_drive_file_id=(.+)$", line.strip())
                    if m:
                        return m.group(1).strip()
        except OSError:
            continue
    return ""


def load_source_transcript_url(job_dir: str) -> str:
    """アップロード済みの補正前原文（Drive .txt）の参照 URL。"""
    hub = _read_google_doc_hub(job_dir)
    url = str(hub.get("source_file_url") or "").strip()
    if url:
        return url
    file_id = str(hub.get("source_drive_file_id") or "").strip()
    if not file_id:
        file_id = _source_id_from_docs_write_log(job_dir)
    return drive_file_view_url(file_id)
