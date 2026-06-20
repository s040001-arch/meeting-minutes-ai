"""会議プロファイル: 全 Claude/GPT 呼び出しで共通使用するコンテキスト。"""

from __future__ import annotations

import json
import os
import re
from typing import Any

MEETING_PROFILE_FILENAME = "meeting_profile.json"

_TRANSCRIPT_SPEAKER_NAME_RE = re.compile(
    r"[\u3040-\u9fff\u30a0-\u30ffA-Za-z]{1,16}(?:さん|様|氏)"
)


def strip_status_prefix(title: str) -> str:
    """Drive Doc 名の【処理中】等の接頭辞を除去する。"""
    return re.sub(r"^【[^】]+】", "", str(title or "").strip()).strip()


def infer_display_title(
    job_dir: str,
    job_id: str,
    filename: str | None = None,
) -> str:
    """旧ジョブ向け: 元ファイル名 stem を推定する（優先順位付き）。"""
    if filename:
        stem = os.path.splitext(os.path.basename(filename))[0].strip()
        if stem:
            return stem

    hub_path = os.path.join(job_dir, "google_doc_hub.json")
    if os.path.isfile(hub_path):
        try:
            with open(hub_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                hub_title = strip_status_prefix(str(data.get("title") or ""))
                if hub_title and not hub_title.startswith("job_"):
                    return hub_title
        except (OSError, json.JSONDecodeError):
            pass

    log_path = os.path.join(job_dir, "e2e_run_log.txt")
    if os.path.isfile(log_path):
        try:
            with open(log_path, encoding="utf-8") as f:
                for line in f:
                    if "input_audio=" not in line:
                        continue
                    raw_path = line.split("input_audio=", 1)[1].strip()
                    stem = os.path.splitext(os.path.basename(raw_path))[0].strip()
                    if stem:
                        return stem
        except OSError:
            pass

    suffix = _job_id_to_filename_stem(job_id)
    if suffix and suffix != job_id:
        return suffix
    return job_id


def resolve_display_title(
    profile: dict | None,
    *,
    job_id: str = "",
    fallback: str | None = None,
) -> str:
    """Doc タイトル用の人間可読文字列を返す。"""
    profile = profile or {}
    display_title = str(profile.get("display_title") or "").strip()
    if display_title:
        return display_title
    if fallback:
        fb = str(fallback).strip()
        if fb:
            return fb
    return job_id


def build_meeting_profile(
    parsed_filename: dict,
    job_context: dict,
    knowledge_memos: list[str],
    display_title: str | None = None,
) -> dict[str, Any]:
    """会議の全Claude/GPT呼び出しで共通使用するプロファイル。"""
    topics = parsed_filename.get("topics") or []
    topic = parsed_filename.get("topic")
    if not topic and topics:
        topic = "、".join(str(t) for t in topics if str(t).strip())

    return {
        "date": parsed_filename.get("date"),
        "customer_name": parsed_filename.get("customer"),
        "topic": topic,
        "participants": list(parsed_filename.get("attendees") or []),
        "meeting_scope": parsed_filename.get("meeting_scope"),
        "filename_raw_tokens": list(parsed_filename.get("raw_tokens") or []),
        "relevant_knowledge": list(knowledge_memos or []),
        "job_context": dict(job_context or {}),
        "display_title": str(display_title or "").strip() or None,
    }


def infer_participants_from_transcript(
    text: str,
    *,
    min_mentions: int = 3,
    max_names: int = 15,
) -> list[str]:
    """Extract frequent 〇〇さん/様/氏 from transcript (job-specific, not a fixed list)."""
    if not str(text or "").strip():
        return []
    from collections import Counter

    counts = Counter(_TRANSCRIPT_SPEAKER_NAME_RE.findall(text))
    out: list[str] = []
    for spoken, cnt in counts.most_common(max_names):
        if cnt < min_mentions:
            break
        base = re.sub(r"(?:さん|様|氏)$", "", str(spoken).strip())
        if base and base not in out:
            out.append(base)
    return out


def augment_profile_with_transcript_participants(
    profile: dict[str, Any] | None,
    text: str,
) -> dict[str, Any]:
    """When profile has no participants, infer from transcript for editor prompts."""
    merged = dict(profile or {})
    if merged.get("participants"):
        return merged
    inferred = infer_participants_from_transcript(text)
    if not inferred:
        return merged
    merged["participants"] = inferred
    merged["participants_source"] = "transcript_inferred"
    return merged


def format_meeting_profile_for_prompt(profile: dict | None) -> str:
    """全Claude呼び出しの冒頭に挿入する共通ヘッダ。"""
    profile = profile or {}
    scope_label = {
        "external": "外部会議（プレセナが顧客に対して提案・相談を行う場）",
        "internal": "社内会議（プレセナ社内の議論）",
        "unknown": "種別不明",
    }.get(str(profile.get("meeting_scope") or "unknown"), "種別不明")

    lines = ["\n\n【この会議について】"]
    if profile.get("date"):
        lines.append(f"- 日付: {profile['date']}")
    if profile.get("customer_name"):
        lines.append(f"- 顧客: {profile['customer_name']}")
    if profile.get("topic"):
        lines.append(f"- 議題: {profile['topic']}")
    participants = profile.get("participants") or []
    if participants:
        lines.append(f"- 参加者: {', '.join(str(p) for p in participants)}")
    lines.append(f"- 会議種別: {scope_label}")
    lines.append(
        "- 想定読者: 相原隆太郎（プレセナ・ストラテジック・パートナーズの提案担当）。"
        "会議で実際に発言された内容を正確に振り返るための議事録。"
    )
    return "\n".join(lines)


def save_meeting_profile(job_dir: str, profile: dict[str, Any]) -> str:
    path = os.path.join(job_dir, MEETING_PROFILE_FILENAME)
    os.makedirs(job_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    return path


def load_meeting_profile(job_dir: str) -> dict[str, Any]:
    path = os.path.join(job_dir, MEETING_PROFILE_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _job_id_to_filename_stem(job_id: str) -> str:
    """job_YYYYMMDD_HHMMSS_<stem> 形式からファイル名 stem を推定する。"""
    m = re.match(r"^job_\d{8}_\d{6}_(.+)$", job_id)
    if m:
        return m.group(1)
    if job_id.startswith("job_"):
        return job_id[4:]
    return job_id


def _profile_has_core_fields(profile: dict[str, Any]) -> bool:
    return bool(
        profile.get("meeting_scope")
        or profile.get("customer_name")
        or profile.get("participants")
        or profile.get("topic")
    )


def ensure_meeting_profile(
    job_dir: str,
    *,
    job_id: str | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """meeting_profile.json が無い／空の旧ジョブ向けにプロファイルを生成して保存する。"""
    jid = job_id or os.path.basename(job_dir)
    existing = load_meeting_profile(job_dir)

    if not str(existing.get("display_title") or "").strip():
        inferred = infer_display_title(job_dir, jid, filename)
        if inferred:
            existing = dict(existing or {})
            existing["display_title"] = inferred
            save_meeting_profile(job_dir, existing)

    if _profile_has_core_fields(existing):
        return existing

    from filename_parser import extract_known_people_from_knowledge, parse_filename
    from job_context import load_job_context, save_job_context
    from knowledge_sheet_store import load_knowledge_memos

    job_context = load_job_context(job_dir)
    try:
        knowledge_memos = load_knowledge_memos() or []
    except Exception:
        knowledge_memos = list(existing.get("relevant_knowledge") or [])

    stem = (filename or "").strip()
    if not stem:
        stem = _job_id_to_filename_stem(job_id or os.path.basename(job_dir))
    if not stem:
        stem = os.path.basename(job_dir)
    parse_name = stem if "." in stem else f"{stem}.txt"

    known_people = extract_known_people_from_knowledge(knowledge_memos)
    parsed_filename = parse_filename(parse_name, known_people)

    jc = dict(job_context or {})
    if parsed_filename.get("attendees") and not jc.get("participants"):
        jc["participants"] = list(parsed_filename["attendees"])
    if parsed_filename.get("customer") and not jc.get("related_companies"):
        jc["related_companies"] = [parsed_filename["customer"]]
    if parsed_filename.get("topics") and not jc.get("agenda"):
        jc["agenda"] = list(parsed_filename["topics"])
    if parsed_filename.get("meeting_scope"):
        jc["meeting_scope"] = parsed_filename["meeting_scope"]
    if parsed_filename.get("date"):
        jc["meeting_date"] = parsed_filename["date"]
    if parsed_filename.get("customer"):
        jc["customer_name"] = parsed_filename["customer"]
    try:
        save_job_context(job_dir, jc)
    except OSError:
        pass

    profile = build_meeting_profile(
        parsed_filename=parsed_filename,
        job_context=jc,
        knowledge_memos=knowledge_memos,
        display_title=str(existing.get("display_title") or "").strip()
        or infer_display_title(job_dir, jid, filename),
    )
    save_meeting_profile(job_dir, profile)
    return profile
