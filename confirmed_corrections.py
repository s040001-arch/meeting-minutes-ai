"""確定済み修正ペアの収集と、最終テキストへの決定論的な強制適用。

2026-08-05 導入。楽天ジョブで「山谷」「湯でみ」「習字」など、ユーザー回答や
自動修正で一度は確定した誤表記が最終文書に残った。原因は、確定情報の記録が
複数のファイル（LINE監査・バッチ回答・トリアージ・ナレッジ自己解決）に
分散しており、品質ゲートが LINE 監査しか見ていなかったこと。

このモジュールはジョブ内の全ての確定ソースからペアを集約し、
- 統合仕上げパスの最後で残存箇所を強制置換（enforce）
- 品質ゲートで残存チェック（collect の結果を渡す）
の両方に使う。LLMは使わない決定論的処理。

安全のため単語レベルのペアのみ扱う（文全体の置換は文脈依存なので対象外）。
"""
from __future__ import annotations

import json
import os
from typing import Any

# 盲目置換してよいペアの上限。これを超えるものは文・句レベルとみなして
# 強制置換しない（誤爆リスクがあるため）。
_MAX_WRONG_LEN = 20
_MAX_RIGHT_LEN = 30
_MIN_WRONG_LEN = 2


def _pair_is_safe(wrong: str, right: str) -> bool:
    if not wrong or not right or wrong == right:
        return False
    if not (_MIN_WRONG_LEN <= len(wrong) <= _MAX_WRONG_LEN):
        return False
    if len(right) > _MAX_RIGHT_LEN:
        return False
    if "。" in wrong or "。" in right or "\n" in wrong or "\n" in right:
        return False
    # wrong が right の部分文字列だと置換が発散する（山屋→山屋さん等）。
    if wrong in right:
        return False
    return True


def _iter_jsonl(path: str):
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def collect_confirmed_pairs(job_dir: str) -> list[dict[str, str]]:
    """ジョブ内の全確定ソースから wrong→right ペアを集約する。

    ソース:
    - line_correction_audit.jsonl … LINE回答から抽出された修正ペア
    - batch_corrections_audit.jsonl … バッチ質問の回答で適用された修正
    - auto_triage_audit.jsonl … 自動トリアージで適用された修正
    - knowledge_self_answer_audit.jsonl … ナレッジによる自己解決
    - auto_corrections.json … 整合性レビューの高確信自動修正（単語レベルのみ）
    """
    pairs: dict[str, dict[str, str]] = {}

    def add(wrong: Any, right: Any, source: str) -> None:
        w = str(wrong or "").strip()
        r = str(right or "").strip()
        if _pair_is_safe(w, r):
            pairs[w] = {"wrong": w, "right": r, "source": source}

    for row in _iter_jsonl(os.path.join(job_dir, "line_correction_audit.jsonl")):
        if isinstance(row, dict):
            add(row.get("wrong"), row.get("correct"), "line_answer")

    for row in _iter_jsonl(
        os.path.join(job_dir, "batch_corrections_audit.jsonl")
    ):
        if not isinstance(row, dict):
            continue
        for a in row.get("applied") or []:
            if isinstance(a, dict) and a.get("action") == "correct":
                add(a.get("before"), a.get("after"), "batch_answer")

    for row in _iter_jsonl(os.path.join(job_dir, "auto_triage_audit.jsonl")):
        if not isinstance(row, dict):
            continue
        for a in row.get("applied") or []:
            if isinstance(a, dict):
                add(a.get("word"), a.get("after"), "auto_triage")

    for row in _iter_jsonl(
        os.path.join(job_dir, "knowledge_self_answer_audit.jsonl")
    ):
        if isinstance(row, dict):
            add(row.get("wrong"), row.get("right"), "knowledge_self_answer")

    auto_path = os.path.join(job_dir, "auto_corrections.json")
    if os.path.isfile(auto_path):
        try:
            with open(auto_path, "r", encoding="utf-8") as f:
                rows = json.load(f)
            if isinstance(rows, list):
                for a in rows:
                    if isinstance(a, dict) and a.get("action") == "correct":
                        add(a.get("before"), a.get("after"), "auto_correction")
        except (OSError, json.JSONDecodeError):
            pass

    return list(pairs.values())


def enforce_confirmed_pairs(
    text: str,
    pairs: list[dict[str, str]],
) -> tuple[str, list[dict[str, Any]]]:
    """確定ペアの誤表記が残っていれば全出現を強制置換する。

    返り値: (適用後テキスト, 適用記録のリスト)
    """
    out = text
    enforced: list[dict[str, Any]] = []
    for pair in pairs:
        wrong = pair.get("wrong") or ""
        right = pair.get("right") or ""
        if not wrong or not right or wrong not in out:
            continue
        # right が既に本文にある場合も wrong の残存は置換対象。
        count = out.count(wrong)
        out = out.replace(wrong, right)
        enforced.append(
            {
                "wrong": wrong,
                "right": right,
                "count": count,
                "source": pair.get("source") or "",
            }
        )
    return out, enforced
