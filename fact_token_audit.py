#!/usr/bin/env python3
"""事実トークンの横断監査（2026-08-07）。

背景: 各適用経路（resolver / dense repair / safe fixes / 整文…）が
それぞれ安全ガードを持つが、実装がバラバラで定義に穴が出る
（例: 単位リスト式の数値保護が「第2希望」の 2 を守れず、段落修復が
第2希望→第1希望と事実を書き換えた）。

このモジュールは2つの役割を持つ:
1. 保護トークン定義の唯一の情報源（数字・漢数字+助数詞・敬称付き人名）。
   各経路のガードはここから import する。
2. 出口の横断検問（sentinel）: パイプラインの入口と出口のテキストで
   保護トークンの多重集合を照合し、確定修正ペアで説明できない差分を
   「違反」として返す。どの経路のガードが破れても、ここで必ず捕まる。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

# 全ての数字列を保護する（単位の有無・桁数を問わない）。
# 52.5 / 7-8 のような区切り付きは1トークンとして扱う。
NUMBER_RE = re.compile(
    r"(?:\d+(?:[.,、〜～-]\d+)*(?:kg|人|店|店舗|日|ヶ月|月|年|行|割|回|社|"
    r"時|分|万円|円|%|クラス|名)|\d+(?:[.,、〜～-]\d+)*)",
    re.IGNORECASE,
)
# 漢数字+助数詞（七割・三ヶ月・十名など）と「第N」（第一志望など）。
# 漢数字単独（一緒・一番の「一」など）は誤検知が多いため対象外。
KANJI_NUMBER_RE = re.compile(
    r"(?:[一二三四五六七八九十百千万]+"
    r"(?:割|人|名|円|万円|年|ヶ月|ヵ月|月|日|時間|時|分|回|社|店|件|個|期|番目)"
    r"|第[0-9一二三四五六七八九十]+)"
)
HONORIFIC_NAME_RE = re.compile(r"[一-龥ァ-ヶA-Za-z]{1,12}(?:さん|様)")


def protected_token_multisets(
    text: str,
) -> tuple[Counter[str], Counter[str], Counter[str]]:
    """(数字, 漢数字+助数詞, 敬称付き人名) の多重集合を返す。"""
    return (
        Counter(NUMBER_RE.findall(text)),
        Counter(KANJI_NUMBER_RE.findall(text)),
        Counter(HONORIFIC_NAME_RE.findall(text)),
    )


def _context(text: str, token: str, width: int = 40) -> str:
    pos = text.find(token)
    if pos < 0:
        return ""
    start = max(0, pos - width)
    end = min(len(text), pos + len(token) + width)
    return text[start:end].replace("\n", " ")


def _explained_by_pairs(
    token: str,
    pairs: list[dict[str, str]],
    side: str,
) -> bool:
    """トークンの出現/消失が確定修正ペア（wrong→correct）で説明できるか。"""
    for pair in pairs:
        surface = str(pair.get(side) or "")
        if token and token in surface:
            return True
    return False


def audit_fact_token_diff(
    before: str,
    after: str,
    allow_pairs: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """入口と出口の保護トークン差分のうち、説明できないものを返す。

    返り値の各要素:
      {"kind": "number|kanji_number|name", "token", "delta",
       "issue", "quote"}
    quote は質問キューに流せるよう、出口テキスト上の周辺文脈
    （消失の場合は入口テキスト上の文脈）を入れる。
    """
    pairs = allow_pairs or []
    violations: list[dict[str, Any]] = []
    before_sets = protected_token_multisets(before)
    after_sets = protected_token_multisets(after)
    kinds = ("number", "kanji_number", "name")
    for kind, b, a in zip(kinds, before_sets, after_sets):
        for token in set(b) | set(a):
            delta = a[token] - b[token]
            if delta == 0:
                continue
            if delta > 0 and _explained_by_pairs(token, pairs, "correct"):
                continue
            if delta < 0 and _explained_by_pairs(token, pairs, "wrong"):
                continue
            src = after if delta > 0 else before
            direction = "出現" if delta > 0 else "消失"
            violations.append(
                {
                    "kind": kind,
                    "token": token,
                    "delta": delta,
                    "issue": (
                        f"処理中に保護トークン（{kind}）が{direction}: "
                        f"「{token}」 x{abs(delta)}。事実が変化した疑い。"
                    ),
                    "quote": _context(src, token),
                }
            )
    return violations
