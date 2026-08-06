"""話者交代位置での改行挿入（本文の文字は1文字も変えない）。

2026-08-07 ユーザー決定: 話者ラベルは付けず（誤帰属は事実誤りになり得る
ため）、話者が交代したと思われる位置での改行だけを徹底する。
1つの段落に両者の発言が混ざって読みにくい問題（単価交渉・雑談等）への対策。

安全設計（決定論ゲート）:
- LLM には改行の挿入のみを指示する。
- 出力から空白・改行を除いた文字列が入力と完全一致しない場合、
  その段落の変更を破棄して原文を使う。改変事故が構造的に起きない。
"""
from __future__ import annotations

import os
import re

_MODEL = "claude-sonnet-5"
# これ未満の段落は複数話者が混ざっている可能性が低いので触らない
_MIN_PARA_LEN = 100
# 1ジョブでの処理段落数の上限（コスト暴走防止）
_MAX_PARAS = 120

_WS_RE = re.compile(r"\s+")


def _normalize(s: str) -> str:
    return _WS_RE.sub("", s or "")


def _split_one(client, para: str) -> str:
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=4000,
        timeout=90,
        system=(
            "入力は会議の発言録の1段落です。複数の話者の発言が混ざっている"
            "場合、話者が交代していると思われる位置で改行を挿入してください。"
            "ルール: (1) 文字の追加・削除・変更は一切禁止。改行の挿入のみ。"
            "(2) 相槌（「うん」「なるほど」等）も交代とみなして改行してよい。"
            "(3) 交代が無ければ入力をそのまま返す。"
            "(4) 出力は本文のみ。説明・前置きは不要。"
        ),
        messages=[{"role": "user", "content": para}],
    )
    out = "".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()
    return out


def apply_speaker_turn_breaks(text: str) -> tuple[str, dict]:
    """長い段落を話者交代で分割する。返り値: (処理後テキスト, 統計)。"""
    stats = {
        "enabled": True,
        "paras_seen": 0,
        "paras_split": 0,
        "paras_discarded": 0,
        "failed": False,
    }
    if os.environ.get("SPEAKER_TURN_BREAKS_ENABLED", "1").strip() == "0":
        stats["enabled"] = False
        return text, stats
    if not os.environ.get("ANTHROPIC_API_KEY"):
        stats["enabled"] = False
        return text, stats

    try:
        import anthropic

        client = anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001
        print(f"speaker_turn_breaks_client_failed={exc!r}")
        stats["failed"] = True
        return text, stats

    paras = text.split("\n\n")
    out_paras: list[str] = []
    processed = 0
    for para in paras:
        stripped = para.strip()
        if len(stripped) < _MIN_PARA_LEN or processed >= _MAX_PARAS:
            out_paras.append(para)
            continue
        # 見出し・箇条書きは触らない
        if stripped.startswith(("#", "-", "・", "※", "▼", "[")):
            out_paras.append(para)
            continue
        stats["paras_seen"] += 1
        processed += 1
        try:
            candidate = _split_one(client, stripped)
        except Exception as exc:  # noqa: BLE001
            print(f"speaker_turn_breaks_llm_failed={exc!r}")
            out_paras.append(para)
            continue
        if not candidate or _normalize(candidate) != _normalize(stripped):
            # 文字が変わった → 破棄（決定論ゲート）
            if candidate:
                stats["paras_discarded"] += 1
            out_paras.append(para)
            continue
        if candidate.count("\n") > 0:
            stats["paras_split"] += 1
            # 段落間と同じ空行区切りにして読みやすくする
            out_paras.append("\n\n".join(
                seg.strip() for seg in candidate.split("\n") if seg.strip()
            ))
        else:
            out_paras.append(para)
    return "\n\n".join(out_paras), stats
