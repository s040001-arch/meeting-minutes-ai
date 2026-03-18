import json
from typing import Any, Dict, List

from utils.logger import logger


def _to_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _stringify_list_items(values: List[Any]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            result.append(text)
    return result


def _to_bullet_items(value: Any) -> List[str]:
    raw_items = _stringify_list_items(_to_list(value))
    bullets: List[str] = []
    for item in raw_items:
        for line in str(item).splitlines():
            text = line.strip()
            if not text:
                continue
            if text.startswith("- "):
                text = text[2:].strip()
            elif text.startswith("・"):
                text = text[1:].strip()
            elif text.startswith("* "):
                text = text[2:].strip()
            if text:
                bullets.append(text)
    return bullets


def _format_bullet_lines(items: List[str]) -> List[str]:
    lines: List[str] = []
    for item in items:
        text = " ".join(str(item).splitlines()).strip()
        if text:
            lines.append(f"- {text}")
    return lines


def _normalize_record_item(item: Any) -> Dict[str, str]:
    if isinstance(item, dict):
        speaker = str(item.get("話者", "")).strip()
        utterance = str(
            item.get("発言")
            or item.get("内容")
            or item.get("text")
            or item.get("utterance")
            or ""
        ).strip()
        utterance = " ".join(utterance.splitlines()).strip()
        return {
            "話者": speaker,
            "発言": utterance,
        }

    text = str(item).strip()
    if not text:
        return {"話者": "", "発言": ""}

    if ":" in text:
        speaker, utterance = text.split(":", 1)
        utterance = " ".join(utterance.splitlines()).strip()
        return {"話者": speaker.strip(), "発言": utterance}

    if "：" in text:
        speaker, utterance = text.split("：", 1)
        utterance = " ".join(utterance.splitlines()).strip()
        return {"話者": speaker.strip(), "発言": utterance}

    return {"話者": "", "発言": " ".join(text.splitlines()).strip()}


def _extract_original_record_lines(record_items: List[Any]) -> List[str]:
    lines: List[str] = []
    for item in record_items:
        normalized = _normalize_record_item(item)
        speaker = normalized["話者"]
        utterance = normalized["発言"]
        if speaker and utterance:
            lines.append(f"{speaker}: {utterance}")
        elif utterance:
            lines.append(utterance)
    return lines


def _build_formatted_record_lines(record_items: List[Any]) -> List[str]:
    lines: List[str] = []
    for item in record_items:
        normalized = _normalize_record_item(item)
        speaker = normalized["話者"]
        utterance = normalized["発言"]
        if not utterance:
            continue
        if speaker:
            lines.append(f"{speaker}: {utterance}")
        else:
            lines.append(utterance)
    return lines


def _validate_record_preservation(original_record_items: List[Any], formatted_record_lines: List[str]) -> List[str]:
    errors: List[str] = []
    original_lines = _extract_original_record_lines(original_record_items)
    normalized_formatted_lines = [line[2:] if line.startswith("- ") else line for line in formatted_record_lines]
    if len(original_lines) != len(normalized_formatted_lines):
        errors.append(f"record_count_mismatch input={len(original_lines)} output={len(normalized_formatted_lines)}")
    compare_count = min(len(original_lines), len(normalized_formatted_lines))
    for idx in range(compare_count):
        if original_lines[idx] != normalized_formatted_lines[idx]:
            errors.append(f"record_changed_or_reordered_at_line={idx + 1}")
    return errors


def format_minutes(minutes: Dict[str, Any]) -> str:
    if isinstance(minutes, str):
        return minutes.strip()

    if not isinstance(minutes, dict):
        return str(minutes or "").strip()

    participants = _to_bullet_items(minutes.get("参加者"))
    summary_items = _to_bullet_items(minutes.get("会議概要"))
    decisions = _to_bullet_items(minutes.get("決まったこと"))
    open_points = _to_bullet_items(minutes.get("残論点"))
    next_actions = _to_bullet_items(minutes.get("Next Action"))
    record_items = _to_list(minutes.get("発言録（逐語）") or minutes.get("発言録"))

    formatted_record_lines = _build_formatted_record_lines(record_items)
    validation_errors = _validate_record_preservation(record_items, formatted_record_lines)

    if validation_errors:
        logger.warning("Minutes formatter record validation failed: " + json.dumps(validation_errors, ensure_ascii=False))
    else:
        logger.info("Minutes formatter record validation passed with utterance order preserved")

    sections: List[str] = []
    sections.append("## 参加者")
    sections.extend(_format_bullet_lines(participants) or ["- なし"])
    sections.append("")
    sections.append("## 会議概要")
    sections.extend(_format_bullet_lines(summary_items) or ["- なし"])
    sections.append("")
    sections.append("## 決まったこと")
    sections.extend(_format_bullet_lines(decisions) or ["- なし"])
    sections.append("")
    sections.append("## 残論点")
    sections.extend(_format_bullet_lines(open_points) or ["- なし"])
    sections.append("")
    sections.append("## Next Action")
    sections.extend(_format_bullet_lines(next_actions) or ["- なし"])
    sections.append("")
    sections.append("## 発言録（逐語）")
    sections.extend(_format_bullet_lines(formatted_record_lines) or ["- なし"])
    return "\n".join(sections).strip()
