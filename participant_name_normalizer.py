"""参加者名の早期正規化パス（2026-08-05 ユーザー指示）。

ファイル名には会社名・参加者（例: 楽天インサイト・山屋様・相原）が明記されて
おり、meeting_profile として最初から利用できる。参加者が誰か分からないまま
全文チェックの中で人名の誤認識に気づくのは非効率かつ不安定なので、
機械補正の直後に「本文中の人名らしき語 × 参加者リスト」を専用に突き合わせ、
音声認識の誤変換（山谷さん→山屋さん 等）を冒頭で正規化する。

安全設計:
- 判定は LLM（文脈つき）。同姓の別人・実在の第三者の可能性があれば keep。
- API 失敗時は何もしない（後段の統合仕上げ監査が従来どおり拾う）。
- 適用内容は participant_normalization_audit.json に全件記録する。
"""

from __future__ import annotations

import difflib
import json
import os
import re
from typing import Any

_NORMALIZER_MODEL = "claude-sonnet-5"

_HONORIFICS = ("さん", "様", "さま", "氏", "くん")
_NAME_TOKEN_RE = re.compile(
    r"([\u4E00-\u9FFF]{1,3}|[\u30A0-\u30FF]{2,5})(さん|様|さま|氏|くん)"
)
_MAX_CANDIDATES = 20
_CONTEXT_CHARS = 60

AUDIT_FILENAME = "participant_normalization_audit.json"


def _strip_honorific(name: str) -> str:
    name = str(name or "").strip()
    for h in _HONORIFICS:
        if name.endswith(h) and len(name) > len(h):
            return name[: -len(h)]
    return name


def _similar_to_participant(base: str, participant_bases: list[str]) -> bool:
    for p in participant_bases:
        if not p:
            continue
        if base == p:
            return False  # 一致は正しい表記
        if base[0] == p[0]:
            return True
        if difflib.SequenceMatcher(None, base, p).ratio() >= 0.5:
            return True
    return False


def _collect_candidates(
    text: str, participant_bases: list[str]
) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for m in _NAME_TOKEN_RE.finditer(text):
        base = m.group(1)
        if base in participant_bases:
            continue
        if not _similar_to_participant(base, participant_bases):
            continue
        entry = candidates.setdefault(
            base, {"count": 0, "contexts": []}
        )
        entry["count"] += 1
        if len(entry["contexts"]) < 2:
            start = max(0, m.start() - _CONTEXT_CHARS)
            entry["contexts"].append(
                text[start : m.end() + _CONTEXT_CHARS].replace("\n", " ")
            )
        if len(candidates) >= _MAX_CANDIDATES:
            break
    return candidates


def _confirm_with_llm(
    candidates: dict[str, dict[str, Any]],
    participants: list[str],
    customer: str,
) -> dict[str, str]:
    """候補ごとに replace 先の参加者名を返す（keep は含めない）。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    import anthropic

    lines = []
    for i, (base, info) in enumerate(candidates.items(), 1):
        ctx = " / ".join(str(c) for c in info["contexts"])[:220]
        lines.append(f"{i}. 「{base}」({info['count']}回) 文脈: {ctx}")
    user_msg = (
        f"参加者（ファイル名由来・確定情報）: {', '.join(participants)}\n"
        + (f"顧客企業: {customer}\n" if customer else "")
        + "本文中の人名らしき語:\n"
        + "\n".join(lines)
    )
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=_NORMALIZER_MODEL,
        max_tokens=1200,
        timeout=60,
        system=(
            "会議の書き起こしに現れる人名らしき語が、参加者リストの人物の"
            "音声認識誤変換かどうかを判定します。参加者リストはファイル名"
            "由来の確定情報です。文脈上その参加者を指しているとほぼ確実な"
            "場合のみ replace とし、正しい表記を返してください。"
            "同姓の別人や、会話に登場する第三者（参加者以外の実在人物）の"
            "可能性があれば keep。迷ったら keep。"
            '出力はJSON配列のみ: [{"index":1,"verdict":"replace|keep",'
            '"replace_with":"参加者名"}]'
        ),
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    )
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return {}
    result: dict[str, str] = {}
    bases = list(candidates.keys())
    participant_bases = [_strip_honorific(p) for p in participants]
    for row in json.loads(m.group(0)):
        try:
            idx = int(row.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if not (1 <= idx <= len(bases)):
            continue
        if str(row.get("verdict") or "").strip().lower() != "replace":
            continue
        target = _strip_honorific(str(row.get("replace_with") or ""))
        # 置換先は必ず参加者リスト内（LLMの創作を防ぐ）
        if target in participant_bases:
            result[bases[idx - 1]] = target
    return result


def normalize_participant_names(
    text: str,
    meeting_profile: dict[str, Any] | None,
    *,
    job_dir: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """本文中の参加者名の誤変換を正規化する。返り値: (新テキスト, 適用記録)。"""
    profile = meeting_profile or {}
    participants = [
        str(p).strip() for p in (profile.get("participants") or []) if str(p).strip()
    ]
    participant_bases = [
        _strip_honorific(p) for p in participants if len(_strip_honorific(p)) >= 2
    ]
    if not participant_bases:
        return text, []

    candidates = _collect_candidates(text, participant_bases)
    if not candidates:
        return text, []

    try:
        replacements = _confirm_with_llm(
            candidates,
            participants,
            str(profile.get("customer_name") or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001
        # 失敗時は何もしない（後段の監査が従来どおり拾う）
        print(f"participant_normalizer_llm_failed={exc!r}")
        return text, []
    if not replacements:
        return text, []

    applied: list[dict[str, Any]] = []
    out = text
    for wrong, right in replacements.items():
        # 敬称つき・独立出現（前後が漢字でない）の両方を置換する
        pattern = re.compile(
            rf"(?<![\u4E00-\u9FFF]){re.escape(wrong)}(?![\u4E00-\u9FFF])"
        )
        new_out, n = pattern.subn(right, out)
        if n > 0:
            out = new_out
            applied.append(
                {"wrong": wrong, "right": right, "count": n}
            )

    if applied and job_dir:
        try:
            with open(
                os.path.join(job_dir, AUDIT_FILENAME), "w", encoding="utf-8"
            ) as handle:
                json.dump(
                    {
                        "participants": participants,
                        "candidates": {
                            k: v["count"] for k, v in candidates.items()
                        },
                        "applied": applied,
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError as exc:
            print(f"participant_normalizer_audit_failed={exc!r}")
    return out, applied
