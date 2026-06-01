"""自動学習済み補正辞書のストレージ層 (Phase 1)。

【目的】
- Step 4.5 整合性レビュー(coherence_review)で auto_fix された誤認識
- LINE Q&A で確定した誤認識
をジョブ横断で自動蓄積し、次回以降の machine 補正で即座に置換される
ようにする。これにより「検出→修正→辞書化→再発防止」のループを閉じる。

【既存辞書との関係】
- data/correction_dict.json (手動): 人間が編集する flat dict
- data/knowledge/learned_corrections.json (自動): 本ファイルが管理する構造化辞書
- mechanical_correct_text.PIXEL_RECOGNIZER_REPLACEMENTS (コード): 静的辞書
3層を併用し、適用順は「手動 → 学習 → Pixel(コード) → その他正規化」。

【スキーマ】
{
  "version": 1,
  "updated_at": "ISO8601",
  "asr_corrections": {
    "<wrong>": {
      "to": "<correct>",
      "confidence": "high",
      "occurrences": 3,
      "first_seen": "YYYY-MM-DD",
      "last_seen": "YYYY-MM-DD",
      "sources": [{"job_id": "...", "via": "coherence_review|line_qa", "ts": "ISO8601"}],
      "examples": ["短い前後文脈、最大3件"]
    }
  }
}
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

DEFAULT_LEARNED_PATH = os.path.join("data", "knowledge", "learned_corrections.json")

# 同一誤認識に対して保存する例示の最大数
MAX_EXAMPLES_PER_ENTRY = 3
MAX_EXAMPLE_LEN = 80

# 異常に短い/長い置換ペアを学習しないためのガード
MIN_WRONG_LEN = 2
MAX_WRONG_LEN = 30
MIN_RIGHT_LEN = 1
MAX_RIGHT_LEN = 30

# 学習しても害がある置換(誤学習を防ぐ)
_BLACKLIST_WRONG = {
    "はい", "うん", "ええ", "そう", "そうですね", "ありがとうございます",
    "なるほど", "わかりました", "了解", "失礼します",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _today_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _validate_pair(wrong: str, right: str) -> tuple[bool, str]:
    if not wrong or not right:
        return False, "empty"
    if wrong == right:
        return False, "same_text"
    if wrong in _BLACKLIST_WRONG:
        return False, "blacklisted_wrong"
    wl, rl = len(wrong), len(right)
    if wl < MIN_WRONG_LEN or wl > MAX_WRONG_LEN:
        return False, f"wrong_len_out_of_range({wl})"
    if rl < MIN_RIGHT_LEN or rl > MAX_RIGHT_LEN:
        return False, f"right_len_out_of_range({rl})"
    # 完全部分関係(wrong が right の部分文字列 or 逆)は誤学習リスク高
    # 例: wrong="再雇用", right="再雇用後" は適用するとループ的に拡張するため避ける
    if wrong in right or right in wrong:
        return False, "substring_overlap"
    return True, "ok"


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".learned_corrections_", suffix=".tmp",
        dir=os.path.dirname(path) or "."
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _empty_store() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": _now_iso(),
        "asr_corrections": {},
    }


def load_store(path: str = DEFAULT_LEARNED_PATH) -> dict[str, Any]:
    if not os.path.isfile(path):
        return _empty_store()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty_store()
        data.setdefault("version", 1)
        data.setdefault("updated_at", _now_iso())
        if not isinstance(data.get("asr_corrections"), dict):
            data["asr_corrections"] = {}
        return data
    except (OSError, json.JSONDecodeError) as e:
        print(f"learned_corrections_load_failed={e!r} -> using empty store")
        return _empty_store()


def load_learned_dict(path: str = DEFAULT_LEARNED_PATH) -> dict[str, str]:
    """mechanical 補正で適用する {wrong: to} のフラット辞書を返す。"""
    store = load_store(path)
    out: dict[str, str] = {}
    for wrong, entry in (store.get("asr_corrections") or {}).items():
        if not isinstance(entry, dict):
            continue
        to = str(entry.get("to") or "").strip()
        if not to:
            continue
        out[str(wrong).strip()] = to
    return out


def add_learned_correction(
    *,
    wrong: str,
    right: str,
    via: str,
    job_id: str,
    example: str = "",
    confidence: str = "high",
    path: str = DEFAULT_LEARNED_PATH,
) -> dict[str, Any]:
    """学習辞書に置換ペアを追加(既存ならカウント更新)。

    via: "coherence_review" | "line_qa"
    返り値: {"action": "added|updated|skipped", "reason": "...", "wrong": ..., "right": ...}
    """
    wrong = (wrong or "").strip()
    right = (right or "").strip()
    ok, reason = _validate_pair(wrong, right)
    if not ok:
        return {"action": "skipped", "reason": reason, "wrong": wrong, "right": right}

    store = load_store(path)
    corrections: dict[str, Any] = store.setdefault("asr_corrections", {})
    entry = corrections.get(wrong)
    today = _today_date()
    now = _now_iso()

    if not isinstance(entry, dict):
        entry = {
            "to": right,
            "confidence": confidence,
            "occurrences": 1,
            "first_seen": today,
            "last_seen": today,
            "sources": [{"job_id": job_id, "via": via, "ts": now}],
            "examples": [example.strip()[:MAX_EXAMPLE_LEN]] if example.strip() else [],
        }
        corrections[wrong] = entry
        action = "added"
    else:
        # 矛盾: 既存の `to` と異なる場合は警告ログのみ(上書きはしない、誤学習防止)
        existing_to = str(entry.get("to") or "").strip()
        if existing_to and existing_to != right:
            return {
                "action": "skipped",
                "reason": f"conflict_existing_to={existing_to}",
                "wrong": wrong,
                "right": right,
            }
        entry["occurrences"] = int(entry.get("occurrences") or 0) + 1
        entry["last_seen"] = today
        sources = entry.setdefault("sources", [])
        if isinstance(sources, list):
            sources.append({"job_id": job_id, "via": via, "ts": now})
            # ソース履歴は最新20件まで保持
            if len(sources) > 20:
                del sources[: len(sources) - 20]
        ex_list = entry.setdefault("examples", [])
        if isinstance(ex_list, list) and example.strip():
            clipped = example.strip()[:MAX_EXAMPLE_LEN]
            if clipped not in ex_list:
                ex_list.append(clipped)
                if len(ex_list) > MAX_EXAMPLES_PER_ENTRY:
                    ex_list[:] = ex_list[-MAX_EXAMPLES_PER_ENTRY:]
        action = "updated"

    store["updated_at"] = now
    _atomic_write_json(path, store)
    return {"action": action, "reason": "ok", "wrong": wrong, "right": right}


def remove_learned_correction(
    wrong: str, path: str = DEFAULT_LEARNED_PATH
) -> bool:
    """誤って学習されたエントリを削除する(手動メンテ用)。"""
    wrong = (wrong or "").strip()
    if not wrong:
        return False
    store = load_store(path)
    corrections = store.get("asr_corrections") or {}
    if wrong not in corrections:
        return False
    del corrections[wrong]
    store["updated_at"] = _now_iso()
    _atomic_write_json(path, store)
    return True


def format_for_print(path: str = DEFAULT_LEARNED_PATH) -> str:
    """CLI 表示用の整形済み文字列を返す。"""
    store = load_store(path)
    corrections = store.get("asr_corrections") or {}
    lines: list[str] = []
    lines.append(f"learned_corrections.json (updated_at={store.get('updated_at')})")
    lines.append(f"entries: {len(corrections)}")
    if not corrections:
        lines.append("  (empty)")
        return "\n".join(lines)
    items = sorted(
        corrections.items(),
        key=lambda kv: (-int(kv[1].get("occurrences", 0)), kv[0]),
    )
    for wrong, entry in items:
        to = entry.get("to", "")
        occ = entry.get("occurrences", 0)
        conf = entry.get("confidence", "?")
        first = entry.get("first_seen", "?")
        last = entry.get("last_seen", "?")
        sources = entry.get("sources") or []
        via_counts: dict[str, int] = {}
        for s in sources:
            if isinstance(s, dict):
                via = str(s.get("via") or "?")
                via_counts[via] = via_counts.get(via, 0) + 1
        via_str = ", ".join(f"{k}:{v}" for k, v in sorted(via_counts.items())) or "?"
        lines.append(
            f"  {wrong!r:30s} -> {to!r:20s} "
            f"occ={occ:3d} conf={conf:6s} first={first} last={last} via={via_str}"
        )
        for ex in (entry.get("examples") or [])[:1]:
            lines.append(f"      ex: {ex}")
    return "\n".join(lines)
