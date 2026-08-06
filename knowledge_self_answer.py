"""ナレッジによる未解決点の自己解決（2026-08-05 ユーザー指摘）。

事前情報（メール等）や蓄積ナレッジに答えが明記されている未解決点まで
ユーザーに質問していた（例: ナレッジに「Udemy Businessを活用」とあるのに
「教材名はUdemyで合っていますか?」と質問）。質問を送る前にナレッジと
突き合わせ、確実に解決できるものは自動で解決して質問数を減らす。

安全設計:
- ナレッジに明記された事実から一意に確定できる場合のみ解決する（LLM判定）。
- 解決は「本文中の誤表記 → 正表記」の全出現置換として適用し、
  wrong が本文に実在することを検証してから置換する。
- 迷う場合・曖昧な場合は解決しない（従来どおり質問に回す）。
- 全件 knowledge_self_answer_audit.jsonl に記録する。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

_MODEL = "claude-sonnet-5"
_SKIP_STATUSES = {"answered", "done", "closed", "resolved", "asked"}
_MAX_ITEMS = 30

AUDIT_FILENAME = "knowledge_self_answer_audit.jsonl"


def _load_knowledge_lines(
    job_dir: str, points: list[dict[str, Any]] | None = None
) -> list[str]:
    lines: list[str] = []
    try:
        from meeting_profile import load_meeting_profile

        profile = load_meeting_profile(job_dir)
        lines = [str(m) for m in (profile.get("relevant_knowledge") or []) if str(m).strip()]
    except Exception as e:  # noqa: BLE001
        print(f"knowledge_self_answer_profile_load_failed={e!r}")

    # 回答カスケード（2026-08-07 ユーザー方針④）: このジョブで既に得た
    # 回答・確定修正も確定知識として使う。1つの回答で閉じられる未解決を、
    # 次の質問を送る前に全部閉じるための供給源。
    for p in points or []:
        if not isinstance(p, dict):
            continue
        if str(p.get("status") or "").strip().lower() not in {
            "answered",
            "done",
        }:
            continue
        quote = str(p.get("anomaly_word") or p.get("text") or "").strip()
        answer = str(p.get("answer") or "").strip()
        if quote and answer:
            lines.append(
                f"確認済み回答: 『{quote[:80]}』について→『{answer[:120]}』"
            )
    try:
        from confirmed_corrections import collect_confirmed_pairs

        for pair in collect_confirmed_pairs(job_dir):
            lines.append(
                f"確定修正: 『{pair['wrong']}』は『{pair['right']}』の誤認識"
            )
    except Exception as e:  # noqa: BLE001
        print(f"knowledge_self_answer_pairs_load_failed={e!r}")

    # 重複除去（順序維持）
    seen: set[str] = set()
    unique: list[str] = []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            unique.append(ln)
    return unique


def _ask_llm(
    knowledge: list[str], items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    import anthropic
    import re

    payload = {
        "knowledge": knowledge,
        "items": [
            {
                "index": i + 1,
                "quote": str(it.get("anomaly_word") or "").strip()
                or str(it.get("text") or "").strip()[:160],
                "reason": str(it.get("reason") or "").strip()[:120],
                "hypothesis": str(it.get("estimated_correction") or "").strip(),
            }
            for i, it in enumerate(items)
        ],
    }
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=2000,
        timeout=90,
        system=(
            "あなたは議事録AIの未解決点トリアージ担当です。"
            "入力は確定済みナレッジ（会議の事前資料・過去の確認回答由来）と、"
            "本文の未解決点一覧です。ナレッジに明記された事実から表記が一意に"
            "確定できる項目のみ resolvable=true とし、本文中の誤表記(wrong)と"
            "正表記(right)を返してください。wrong は quote 内に実在する文字列"
            "そのままにしてください。ナレッジに根拠がない推測、意味・事実が"
            "変わりうる修正、数値の変更は resolvable=false にしてください。"
            "出力はJSON配列のみ: "
            '[{"index":1,"resolvable":true,"wrong":"湯でみ","right":"Udemy",'
            '"basis":"ナレッジの根拠を短く"}]'
        ),
        messages=[
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
        ],
    )
    body = "".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    )
    m = re.search(r"\[.*\]", body, re.DOTALL)
    if not m:
        return []
    rows = json.loads(m.group(0))
    return [r for r in rows if isinstance(r, dict)]


def resolve_unknowns_with_knowledge(
    *, unknowns_path: str, text_path: str, job_dir: str
) -> int:
    """ナレッジで確実に解決できる未解決点を解決する。返り値: 解決数。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return 0
    if not (unknowns_path and os.path.isfile(unknowns_path)):
        return 0
    if not (text_path and os.path.isfile(text_path)):
        return 0

    with open(unknowns_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    points = [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    pending = [
        p
        for p in points
        if str(p.get("status") or "").strip().lower() not in _SKIP_STATUSES
    ][:_MAX_ITEMS]
    if not pending:
        return 0

    knowledge = _load_knowledge_lines(job_dir, points)
    if not knowledge:
        return 0

    try:
        rows = _ask_llm(knowledge, pending)
    except Exception as e:  # noqa: BLE001
        print(f"knowledge_self_answer_llm_failed={e!r}")
        return 0
    if not rows:
        return 0

    with open(text_path, "r", encoding="utf-8") as f:
        text = f.read()

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_rows: list[dict[str, Any]] = []
    resolved = 0
    for row in rows:
        if not row.get("resolvable"):
            continue
        try:
            idx = int(row.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if not (1 <= idx <= len(pending)):
            continue
        wrong = str(row.get("wrong") or "").strip()
        right = str(row.get("right") or "").strip()
        if len(wrong) < 2 or not right or wrong == right or wrong in right:
            continue
        count = text.count(wrong)
        if count <= 0:
            continue
        text = text.replace(wrong, right)
        point = pending[idx - 1]
        point["status"] = "answered"
        point["answer"] = right
        point["answered_by_question_id"] = "knowledge_self_answer"
        point["answered_at"] = now_iso
        point["auto_applied"] = True
        resolved += 1
        audit_rows.append(
            {
                "at": now_iso,
                "wrong": wrong,
                "right": right,
                "count": count,
                "basis": str(row.get("basis") or "")[:160],
                "anomaly_id": point.get("anomaly_id"),
            }
        )

    if resolved <= 0:
        return 0

    with open(text_path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(unknowns_path, "w", encoding="utf-8") as f:
        json.dump(points, f, ensure_ascii=False, indent=2)
    try:
        audit_path = os.path.join(job_dir, AUDIT_FILENAME)
        with open(audit_path, "a", encoding="utf-8") as f:
            for r in audit_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"knowledge_self_answer_audit_failed={e!r}")
    print(
        "knowledge_self_answer_applied="
        + json.dumps(audit_rows, ensure_ascii=False)[:500]
    )
    return resolved
