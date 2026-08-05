"""影響度ベースの質問クラスタリング。

ユーザー方針（2026-08-05 確定）:
- 品質が最優先、ユーザーの効率が第二優先。質問数は上限で削るのではなく、
  設計の結果として最小化する。
- 「この質問に答えれば文書全体への影響が大きい」ものから先に聞く。
  回答が得られたら一度修正（カスケード）をかけ、残った問題から
  また影響最大のものを聞く、の繰り返し。
- 1通にまとめるのは件数上限ではなく「同じ答えで解決する一群」
  （同一人物・同一語の表記ゆれなど）だけ。

このモジュールは未回答の findings/unknown_points を
「同じ答えで解決しそうな一群」にクラスタリングし、
文書全体への影響度（出現回数×類似項目数）でスコア付けして返す。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

# 敬称は表記ゆれクラスタリングの妨げになるので正規化時に落とす
_HONORIFIC_RE = re.compile(r"(さん|様|氏|くん|ちゃん|さま)$")
# 質問対象の引用文から「答えが波及する核」になりそうな語を抜く:
# カタカナ連続（固有名詞・外来語）と漢字連続（人名・専門語）
_SALIENT_RE = re.compile(r"[\u30A0-\u30FF]{2,}|[\u4E00-\u9FFF]{2,4}")
# ありふれた漢字語は「同じ答えで解決する」根拠にならないため除外
_COMMON_WORDS = {
    "議事録", "会議", "確認", "対応", "検討", "説明", "資料", "共有",
    "今回", "今後", "本日", "現状", "状況", "内容", "部分", "場合",
}

_SIMILARITY_THRESHOLD = 0.6


def _normalize_token(word: Any) -> str:
    w = unicodedata.normalize("NFKC", str(word or "").strip())
    return _HONORIFIC_RE.sub("", w)


def _salient_tokens(text: str) -> list[str]:
    """引用文から、回答が他の箇所へ波及しうる核の語を抜き出す。"""
    tokens: list[str] = []
    for m in _SALIENT_RE.finditer(text):
        tok = m.group(0)
        if tok in _COMMON_WORDS or tok in tokens:
            continue
        tokens.append(tok)
    return tokens


def _tokens_similar(a: str, b: str) -> bool:
    """「シュニア/シニア」のような同一対象の表記ゆれを同じ群と見なす。"""
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= _SIMILARITY_THRESHOLD


def _point_tokens(point: dict[str, Any]) -> list[str]:
    word = _normalize_token(
        point.get("anomaly_word") or point.get("text") or ""
    )
    if not word:
        return []
    # 短い語（語レベルの崩れ）はそれ自体が核。文レベルの引用は核語を抽出。
    if len(word) <= 10:
        return [word]
    tokens = _salient_tokens(word)
    return tokens if tokens else [word]


def _points_related(tokens_a: list[str], tokens_b: list[str]) -> bool:
    return any(
        _tokens_similar(a, b) for a in tokens_a for b in tokens_b
    )


def _position_of(point: dict[str, Any]) -> int:
    try:
        pos = int(point.get("context_position_in_transcript"))
        return pos if pos >= 0 else 10**9
    except (TypeError, ValueError):
        return 10**9


def cluster_pending_findings(
    points: list[dict[str, Any]],
    full_text: str = "",
) -> list[dict[str, Any]]:
    """未回答項目を「同じ答えで解決する一群」に束ね、影響度順に返す。

    返り値: [{"items": [...], "tokens": [...], "score": int, "first_pos": int}]
    score = 核語の本文中出現回数の合計 + 類似項目ボーナス。
    同点は文書の前方が先（序盤の回答ほどカスケードが遠くまで効く）。
    """
    clusters: list[dict[str, Any]] = []
    for point in points:
        tokens = _point_tokens(point)
        if not tokens:
            tokens = [""]
        target = None
        for cluster in clusters:
            if _points_related(tokens, cluster["tokens"]):
                target = cluster
                break
        if target is None:
            target = {"items": [], "tokens": []}
            clusters.append(target)
        target["items"].append(point)
        for tok in tokens:
            if tok and tok not in target["tokens"]:
                target["tokens"].append(tok)

    for cluster in clusters:
        occurrences = 0
        for tok in cluster["tokens"]:
            if not tok:
                continue
            occurrences += max(full_text.count(tok), 1) if full_text else 1
        # 類似項目が複数あること自体が「1つの答えで複数解決」の証拠
        cluster["score"] = occurrences + 3 * (len(cluster["items"]) - 1)
        cluster["first_pos"] = min(
            _position_of(p) for p in cluster["items"]
        )
        cluster["items"].sort(key=_position_of)

    clusters.sort(key=lambda c: (-c["score"], c["first_pos"]))
    return clusters
