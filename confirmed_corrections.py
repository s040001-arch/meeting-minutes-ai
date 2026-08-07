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
import re
from typing import Any

# 盲目置換してよいペアの上限。これを超えるものは文・句レベルとみなして
# 強制置換しない（誤爆リスクがあるため）。
_MAX_WRONG_LEN = 20
_MAX_RIGHT_LEN = 30
_MIN_WRONG_LEN = 2


# 2026-08-07 GPT監査#5対応: 列挙マーカーや数字・記号だけの wrong は
# 全文置換すると小数・箇条書き等の無関係箇所を巻き込む（「2.」混入事故の
# 増幅経路）。日本語・英字を含まないペアは強制置換の対象にしない。
_ENUM_LIKE_RE = re.compile(r"^[0-9０-９]{1,3}[.．、)）]?$")
_HAS_WORD_CHAR_RE = re.compile(r"[ぁ-んァ-ヶ一-龥A-Za-z]")


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
    # 列挙マーカー（2. / ３） 等）や語文字を含まない wrong は誤爆リスク。
    if _ENUM_LIKE_RE.match(wrong) or not _HAS_WORD_CHAR_RE.search(wrong):
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


# 確定領域とみなす after テキストの最小長。短い語（山屋さん等）は
# 語レベルの修正であり、その語を含む文全体をロックすると過剰なため除外。
_REGION_MIN_LEN = 15


def collect_confirmed_region_texts(job_dir: str) -> list[str]:
    """人間の回答・自動適用で確定した「修正後の文」（確定領域）を集める。

    2026-08-06 導入。回答が適用されると本文は修正後の文になるが、再監査は
    その修正後の文を再び違和感として検出し、再質問していた（構造的欠陥:
    人間の確定に対する終局性が無い）。ここで集めた確定領域に重なる検出は、
    以後質問対象にしない。
    """
    texts: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        s = str(value or "").strip()
        if len(s) >= _REGION_MIN_LEN and s not in seen:
            seen.add(s)
            texts.append(s)

    for row in _iter_jsonl(os.path.join(job_dir, "line_correction_audit.jsonl")):
        if isinstance(row, dict):
            add(row.get("correct"))

    for row in _iter_jsonl(
        os.path.join(job_dir, "batch_corrections_audit.jsonl")
    ):
        if not isinstance(row, dict):
            continue
        for a in row.get("applied") or []:
            if isinstance(a, dict):
                add(a.get("after"))

    for row in _iter_jsonl(os.path.join(job_dir, "auto_triage_audit.jsonl")):
        if not isinstance(row, dict):
            continue
        for a in row.get("applied") or []:
            if isinstance(a, dict):
                add(a.get("after"))

    for row in _iter_jsonl(
        os.path.join(job_dir, "knowledge_self_answer_audit.jsonl")
    ):
        if isinstance(row, dict):
            add(row.get("right"))

    auto_path = os.path.join(job_dir, "auto_corrections.json")
    if os.path.isfile(auto_path):
        try:
            with open(auto_path, "r", encoding="utf-8") as f:
                rows = json.load(f)
            if isinstance(rows, list):
                for a in rows:
                    if isinstance(a, dict):
                        add(a.get("after"))
        except (OSError, json.JSONDecodeError):
            pass

    # 回答済み unknown_points の確定内容（仮説への「はい」等）
    unknowns_path = os.path.join(job_dir, "unknown_points.json")
    if os.path.isfile(unknowns_path):
        try:
            with open(unknowns_path, "r", encoding="utf-8") as f:
                points = json.load(f)
            if isinstance(points, list):
                for p in points:
                    if not isinstance(p, dict):
                        continue
                    if str(p.get("status") or "").lower() not in {
                        "answered",
                        "done",
                        "closed",
                        "resolved",
                    }:
                        continue
                    add(p.get("estimated_correction"))
                    # 回答テキスト内の確定表現（プレフィックスを剥がす）
                    ans = str(p.get("answer") or "").strip()
                    if ans.startswith("自動適用"):
                        ans = ans.split(":", 1)[-1].strip()
                    add(ans)
        except (OSError, json.JSONDecodeError):
            pass

    return texts


def enforce_confirmed_pairs(
    text: str,
    pairs: list[dict[str, str]],
) -> tuple[str, list[dict[str, Any]]]:
    """確定ペアの誤表記が残っていれば全出現を強制置換する。

    返り値: (適用後テキスト, 適用記録のリスト)
    """
    # この層は「確定済み修正が最終文に必ず反映される」ことを保証する
    # 決定論的セーフティネット。ネットワーク・LLM 依存を持ち込まない。
    # 実在語の別文脈への誤爆は、上流のバッチ回答経路（recognition_batch）
    # で scope 判定により抑止済み。ここに来るペアは _pair_is_safe と
    # 列挙マーカーガードを通過した確定ペアのみ。
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
