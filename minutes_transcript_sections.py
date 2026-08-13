"""Safely publish a transcript with its per-section summary headings."""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meeting_profile import load_meeting_profile
from transcript_section_summarizer import add_section_headings


TRANSCRIPT_MARKER = "## 発言録（整文）"
_HEADING_RE = re.compile(r"(?m)^### ▼.+$")
_STRUCTURED_SUFFIX_MARKERS = (
    "## 管理情報",
    "## 確認ワークスペース",
)


def generate_sectioned_transcript(
    *,
    job_dir: str | Path,
    transcript_text: str,
    minimum_headings: int = 2,
) -> str:
    """Generate section summaries and fail closed if none were produced."""
    job = Path(job_dir)
    profile: dict[str, Any] = load_meeting_profile(str(job))
    annotated = add_section_headings(transcript_text, profile).strip()
    count = len(_HEADING_RE.findall(annotated))
    if count < minimum_headings:
        raise RuntimeError(
            "section summary generation produced too few headings: "
            f"{count} < {minimum_headings}"
        )
    return annotated + "\n"


def _backup(path: Path, stamp: str) -> None:
    if path.is_file():
        shutil.copy2(
            path,
            path.with_name(f"{path.stem}_pre_section_restore_{stamp}{path.suffix}"),
        )


def _split_structured_suffix(remainder: str) -> tuple[str, str]:
    candidates = [
        (remainder.find(marker), marker)
        for marker in _STRUCTURED_SUFFIX_MARKERS
        if remainder.find(marker) >= 0
    ]
    if not candidates:
        return remainder, ""
    idx, _ = min(candidates, key=lambda pair: pair[0])
    return remainder[:idx], "\n\n" + remainder[idx:].lstrip()


def write_sectioned_transcript_artifacts(
    *,
    job_dir: str | Path,
    annotated_transcript: str,
    marker: str = TRANSCRIPT_MARKER,
) -> dict[str, Any]:
    """Replace transcript blocks in draft/structured while preserving metadata."""
    job = Path(job_dir)
    draft_path = job / "minutes_draft.md"
    structured_path = job / "minutes_structured.md"
    if not draft_path.is_file() or not structured_path.is_file():
        raise FileNotFoundError("minutes_draft.md and minutes_structured.md are required")

    draft = draft_path.read_text(encoding="utf-8")
    structured = structured_path.read_text(encoding="utf-8")
    if marker not in draft or marker not in structured:
        raise RuntimeError(f"transcript marker missing: {marker}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    _backup(draft_path, stamp)
    _backup(structured_path, stamp)

    draft_prefix, _ = draft.split(marker, 1)
    structured_prefix, structured_remainder = structured.split(marker, 1)
    _, structured_suffix = _split_structured_suffix(structured_remainder)
    body = annotated_transcript.strip()

    draft_path.write_text(
        draft_prefix.rstrip() + "\n\n" + marker + "\n\n" + body + "\n",
        encoding="utf-8",
    )
    structured_path.write_text(
        structured_prefix.rstrip()
        + "\n\n"
        + marker
        + "\n\n"
        + body
        + structured_suffix,
        encoding="utf-8",
    )
    return {
        "heading_count": len(_HEADING_RE.findall(body)),
        "draft_path": str(draft_path),
        "structured_path": str(structured_path),
        "backup_stamp": stamp,
    }
