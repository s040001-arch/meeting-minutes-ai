"""逐語録の後処理（話者交代空行分割など）をジョブ単位で実行する。"""
from __future__ import annotations

import os

from diarize_speakers import diarize_transcript
from filename_hints import extract_filename_hints
from job_context import load_job_context


def diarize_transcript_for_job(
    text: str,
    *,
    job_id: str,
    input_root: str = "data/transcriptions",
    model: str | None = None,
) -> str:
    job_dir = os.path.join(input_root, job_id)
    return diarize_transcript(
        text,
        filename_hints=extract_filename_hints(job_id),
        job_context=load_job_context(job_dir),
        model=model,
    )


def diarize_transcript_file_for_job(
    *,
    job_id: str,
    input_path: str,
    output_path: str | None = None,
    input_root: str = "data/transcriptions",
    model: str | None = None,
) -> str:
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"input not found: {input_path}")
    with open(input_path, "r", encoding="utf-8") as f:
        text = f.read()
    result = diarize_transcript_for_job(
        text,
        job_id=job_id,
        input_root=input_root,
        model=model,
    )
    out_path = output_path or input_path
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result)
    return out_path
