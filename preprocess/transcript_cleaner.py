import re
from typing import List

from utils.logger import logger


def _normalize_line_for_compare(line: str) -> str:
    return re.sub(r"\s+", "", line).strip()


def _split_lines(text: str) -> List[str]:
    return [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def _clean_line(line: str) -> str:
    cleaned = line.replace("　", " ")
    cleaned = re.sub(r"[ 	]+", " ", cleaned)
    return cleaned.strip()


def _validate_no_reordering_or_loss(original_lines: List[str], cleaned_lines: List[str]) -> None:
    original_non_empty = [_normalize_line_for_compare(line) for line in original_lines if line.strip()]
    cleaned_non_empty = [_normalize_line_for_compare(line) for line in cleaned_lines if line.strip()]

    if len(original_non_empty) != len(cleaned_non_empty):
        raise ValueError("Transcript cleaning validation failed: line count changed.")

    for index, (original, cleaned) in enumerate(zip(original_non_empty, cleaned_non_empty)):
        if original != cleaned:
            raise ValueError(
                f"Transcript cleaning validation failed: order/content changed at line {index + 1}."
            )


def clean_transcript(transcript_text: str) -> str:
    if not isinstance(transcript_text, str):
        raise TypeError("transcript_text must be a string.")

    logger.info("Starting transcript cleaning with order-preserving validation")

    original_lines = _split_lines(transcript_text)
    cleaned_lines: List[str] = []

    for line in original_lines:
        if not line.strip():
            cleaned_lines.append("")
            continue

        cleaned_line = _clean_line(line)
        cleaned_lines.append(cleaned_line)

    _validate_no_reordering_or_loss(original_lines, cleaned_lines)

    cleaned_transcript = "\n".join(cleaned_lines).strip()

    logger.info("Transcript cleaning completed with order preserved")

    return cleaned_transcript
