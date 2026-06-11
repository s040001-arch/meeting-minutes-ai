"""不明点候補から「相原に聞いても答えられない論点」を除外する共通フィルタ。

議事録の精度向上のための質問は、音声認識ゆれ・固有名詞など
相原本人が今答えを持っているものに限る。
会議で『未定』『検討中』『AかBかで迷っている』等と記録されている論点は、
議事録にそのまま書けば足りており、質問しても『決まっていない』としか答えられない。
"""
from __future__ import annotations

import re

# text / reason / evidence のいずれかに含まれたら「未回答可能」とみなす語
_NON_ANSWERABLE_MARKERS: tuple[str, ...] = (
    "未定",
    "未決定",
    "未確定",
    "決まっていない",
    "決めていない",
    "決まってない",
    "決まっておりません",
    "これから決",
    "今後検討",
    "検討中",
    "検討させ",
    "検討の余地",
    "持ち帰",
    "顧客に確認",
    "先方に確認",
    "先方に聞",
    "お客様に確認",
    "確認する必要",
    "確認いただ",
    "確認してもら",
    "確認させて",
    "次回まで",
    "別途",
    "保留",
    "方向性のみ",
    "方向感",
    "最終的な時期",
    "最終時期",
    "正式な時期",
    "最終判断",
    "落としどころ",
    "落とす",
    "現時点では",
    "現段階では",
    "まだ候補",
    "候補は決",
    "どの3社",
    "どちらを想定",
    "どちらで進",
    "どちらを中心",
    "どちらかで",
    "A案",
    "B案",
    "来期",
    "来年度",
    "次年度",
    "予算交渉",
    "値上げ交渉",
    "値下げ交渉",
    "交渉方針",
    "内示",
    "具体化する必要",
    "具体化する",
    "いけるかな",
    "いけそう",
    "段階的に",
    "社内で",
    "上司に",
    "うちの都合",
    "相手の動き",
    "相手方の動き",
    "承知しました",
    "持ち帰り",
    "伺ったり",
    "その時は",
    "その時に",
)

# 文字起こし誤りではなく、未決定のビジネス判断・方向感の論点
_NEGOTIATION_OR_DIRECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"(?:来期|来年|次年度|今期以降).*(?:予算|交渉|値上|値下|内示|落とし)",
        r"(?:交渉|落としどころ|内示|値上げ|値下げ).*(?:予算|金額|万円|価格|指名)",
        r"(?:70|75|85|58)万.*(?:交渉|いける|落とし|予算|内示|方向)",
        r"(?:予算|金額|価格).*(?:交渉|相談|内示|落とし|方向感|段階)",
        r"(?:早めに|結構早め).*(?:抑え|確保|押さ|予約).*(?:講師|宮本|指名)",
        r"(?:いつ|何月|何日|具体的).*(?:抑え|予約|確保|押さ).*(?:講師|宮本)",
        r"(?:宮本|講師).*(?:早め|優先|抑え|確保).*(?:時期|タイミング|いつ|日程)",
    )
)

# 生成質問が「会議で決めていないことを具体化・確定させようとしている」パターン
_QUESTION_SEEKS_UNDECIDED_COMMITMENT: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"(?:来期|来年|次年度).*(?:予算|交渉|値上|値下|70万|85万|58万|75万)",
        r"(?:交渉|落としどころ|内示|方向感).*(?:合っ|認識|進め|想定)",
        r"(?:合っていますか|で合って|認識で|想定で).*(?:交渉|予算|来期|値上|値下|70万|85万|58万|75万|内示)",
        r"(?:具体的に|正確には|いつまでに|何月|何日).*(?:抑え|予約|確保|押さ|宮本|講師)",
        r"(?:早めに).*(?:抑え|予約).*(?:いつ|時期|タイミング|教え)",
        r"(?:希望|想定).*(?:教えて|お聞かせ|確認).*(?:交渉|予算|来期|値上|内示)",
    )
)

_NON_ANSWERABLE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"最終(?:的)?(?:な)?(?:時期|日程|スケジュール).*(?:未定|未決|決ま)",
        r"(?:未定|未決|決ま).*(?:最終|正式)",
        r"(?:10月|11月|12月|年末|年始|夏|秋|春).*(?:または|／|/|か).*(?:10月|11月|12月|年末|年始|夏|秋|春|年初)",
        r"(?:検討|迷|未定).*(?:または|／|/|か)",
        r"(?:実施|導入|開始|研修|トライアル).*(?:時期|タイミング|日程).*(?:未定|未決|検討|どちら|希望)",
        r"(?:時期|タイミング|日程).*(?:未定|未決|検討中|どちら|希望.*教)",
        r"(?:どちら|どっち).*(?:想定|中心|進め|優先|希望)",
        r"(?:希望|想定).*(?:教えて|お聞かせ|確認|お知らせ)",
    )
)


def item_text_blob(item: dict) -> str:
    parts = [
        str(item.get("text") or ""),
        str(item.get("reason") or ""),
        str(item.get("evidence") or ""),
        str(item.get("hypothesis") or ""),
    ]
    return " ".join(p.strip() for p in parts if p and p.strip())


def is_non_answerable_unknown(item: dict) -> bool:
    """会議時点で答えが存在せず、相原に聞いても無意味な論点か。"""
    if not isinstance(item, dict):
        return False
    blob = item_text_blob(item)
    if not blob:
        return False
    if any(m in blob for m in _NON_ANSWERABLE_MARKERS):
        return True
    if any(p.search(blob) for p in _NEGOTIATION_OR_DIRECTION_PATTERNS):
        return True
    return any(p.search(blob) for p in _NON_ANSWERABLE_PATTERNS)


def filter_answerable_unknown_points(items: list[dict]) -> tuple[list[dict], int]:
    """answerable な候補だけ残す。返り値: (filtered, dropped_count)。"""
    kept: list[dict] = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        if is_non_answerable_unknown(item):
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def question_targets_non_answerable_topic(
    question_text: str,
    selected_unknown: dict | None = None,
) -> bool:
    """生成された質問文が、未決定論点をユーザーに決めさせようとしていないか。"""
    if selected_unknown and is_non_answerable_unknown(selected_unknown):
        return True
    q = str(question_text or "").strip()
    if not q:
        return False
    if is_non_answerable_unknown({"text": q, "reason": "", "evidence": ""}):
        return True
    if any(p.search(q) for p in _QUESTION_SEEKS_UNDECIDED_COMMITMENT):
        return True
    # 「10月中心か年末年始か、希望を教えて」系
    if re.search(r"(どちら|どっち|.*か.*(?:中心|想定|進め|優先))", q):
        if re.search(r"(時期|タイミング|日程|スケジュール|実施|研修|トライアル|導入)", q):
            return True
    if re.search(r"(希望|想定).*(?:教えて|お聞かせ|確認|お知らせ)", q):
        if re.search(r"(時期|タイミング|10月|11月|12月|年末|年始|夏|秋|交渉|予算|来期|値上|内示)", q):
            return True
    return False
