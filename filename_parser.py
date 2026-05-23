"""ファイル名の構造化パーサー。

入力ファイル名のフォーマット規約:

    <日付>_<顧客名 or 「プレセナ」>_<会議内容と参加者...>.<ext>

例:
    20260411_野村不動産_営業スキルアップ研修_川口_相原.m4a
        → 日付=2026-04-11, 顧客=野村不動産, 会議内容=営業スキルアップ研修, 参加者=[川口, 相原]
    20260411_プレセナ_週次定例_相原_中溝.m4a
        → 日付=2026-04-11, 顧客=None(社内会議), 参加者=[相原, 中溝]
    20260411_プレセナ_企画会議.m4a
        → 参加者なしでも可（topic だけ）

【パースルール】
1. 1番目: 4〜8桁の数字 → 日付として解釈（YYYYMMDD / YYMMDD / MMDD）
2. 2番目: 顧客名 or 「プレセナ」（カタカナ・漢字混在可）
   - 「プレセナ」「precena」「内部」「Internal」等のキーワード → 社内会議
   - それ以外 → 外部会議（その文字列＝顧客名）
3. 3番目以降: 会議内容＋参加者（混在可、構造化推論はナレッジと突合せて行う）

ナレッジに既知の人名がある場合は、3番目以降のうち人名と一致するものを参加者として
取り出し、残りを「会議内容/トピック」として扱う。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_BRACKETED_RE = re.compile(r"[【\[\(（][^】\]\)）]*[】\]\)）]")
_SPLIT_RE = re.compile(r"[_\-\s\u3000・/]+")
_DATE_TOKEN_RE = re.compile(r"^\d{4,8}$")

# 「プレセナ」を意味するトークン群（社内会議の判定キー）
# 部分マッチ（startswith）も併用するため、ここはコアキーワードのみを定義する
_INTERNAL_MARKERS = {
    "プレセナ",
    "ぷれせな",
    "precena",
    "internal",
    "社内",
    "内部",
    "弊社",
    "自社",
}

# 「プレセナ社」「プレセナ・ストラテジック・パートナーズ」「Precena Inc」のように
# プレセナを含む派生形もすべて内部判定の対象にする（前方一致）
_INTERNAL_PREFIXES = (
    "プレセナ",
    "ぷれせな",
    "precena",
)

_GENERIC_TOKENS = {
    "打合せ",
    "打ち合わせ",
    "会議",
    "ミーティング",
    "mtg",
    "meeting",
    "議事録",
    "メモ",
    "memo",
    "録音",
    "音声",
    "記録",
    "レコーディング",
    "recording",
    "文字起こし",
    "transcription",
    "transcript",
    "rev",
}


def _is_date_token(s: str) -> bool:
    return bool(_DATE_TOKEN_RE.fullmatch(s.strip()))


def _normalize_date(token: str) -> str:
    """YYYYMMDD/YYMMDD/MMDD を YYYY-MM-DD 形式に正規化。失敗時は原文字列。"""
    s = token.strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) == 6 and s.isdigit():
        # YYMMDD と仮定
        return f"20{s[0:2]}-{s[2:4]}-{s[4:6]}"
    if len(s) == 4 and s.isdigit():
        # MMDD と仮定（年は不定なので付けない）
        return f"--{s[0:2]}-{s[2:4]}"
    return s


def _try_consume_date_tokens(tokens: list[str]) -> tuple[dict[str, str | None], int]:
    """先頭の数値トークン群から日付を構築し、消費したトークン数とともに返す。

    対応パターン:
    - YYYY_MMDD       (2026_0512)  → 2026-05-12, consumed=2
    - YYYYMMDD        (20260512)   → 2026-05-12, consumed=1
    - YYMMDD          (260512)     → 2026-05-12, consumed=1
    - MMDD            (0512)       → --05-12,   consumed=1
    - 日付トークンなし                          → consumed=0
    """
    if not tokens:
        return {"date_raw": None, "date": None}, 0

    first = tokens[0].strip()
    if not _is_date_token(first):
        return {"date_raw": None, "date": None}, 0

    # YYYY_MMDD パターン: 4桁年 + 4桁月日
    if len(first) == 4 and first.isdigit() and len(tokens) >= 2:
        second = tokens[1].strip()
        if (
            _is_date_token(second)
            and len(second) == 4
            and second.isdigit()
            and 1 <= int(second[0:2]) <= 12
            and 1 <= int(second[2:4]) <= 31
            and 2000 <= int(first) <= 2099  # 年の妥当性
        ):
            combined = f"{first}{second}"
            return {
                "date_raw": combined,
                "date": _normalize_date(combined),
            }, 2

    # 単一トークンの日付
    return {
        "date_raw": first,
        "date": _normalize_date(first),
    }, 1


def _is_internal_marker(token: str) -> bool:
    """社内会議を意味するトークンかを判定する。

    完全一致だけでなく、「プレセナ社」「プレセナ・ストラテジック・パートナーズ」
    「Precena Inc」のような派生形（前方一致）も内部扱いにする。
    """
    s = token.strip().casefold()
    if not s:
        return False
    # 1) 完全一致
    if s in {m.casefold() for m in _INTERNAL_MARKERS}:
        return True
    # 2) 前方一致（"プレセナ社", "プレセナ・ストラテジック・パートナーズ", "Precena Inc" 等）
    for prefix in _INTERNAL_PREFIXES:
        if s.startswith(prefix.casefold()):
            return True
    return False


_PERSON_NAME_TOKEN_RE = re.compile(
    r"^[一-龠々]{1,4}(?:さん|氏|様)?$"
)


def _looks_like_person_name_token(token: str) -> bool:
    """ファイル名トークンが人名らしいか（ナレッジ未登録でも判定）。"""
    t = token.strip()
    if not t or len(t) > 8:
        return False
    if t.casefold() in _GENERIC_TOKENS:
        return False
    if _PERSON_NAME_TOKEN_RE.fullmatch(t):
        return True
    # 英字名（松本→漢字のみだが短い固有名詞も拾う）
    if re.fullmatch(r"[A-Za-z]{2,12}", t):
        return True
    return False


def _split_attendees_from_topic(
    tokens: list[str],
    known_people: set[str],
) -> tuple[list[str], list[str]]:
    """3番目以降のトークン群を「会議内容」と「参加者」に分離する。

    判定:
    - known_people（ナレッジ等から取得した既知の人名集合）に一致するトークン → 参加者
    - 人名らしい短いトークン（山崎様、松本 等）→ 参加者
    - それ以外 → 会議内容/トピック側
    """
    attendees: list[str] = []
    topics: list[str] = []
    for t in tokens:
        t = t.strip()
        if not t or len(t) <= 1:
            continue
        if t.casefold() in _GENERIC_TOKENS:
            continue
        if t in known_people:
            attendees.append(t)
            continue
        # 「川口さん」「相原氏」「山崎様」のような呼称付きも参加者扱い
        stripped = re.sub(r"(さん|氏|様)$", "", t)
        if stripped in known_people:
            attendees.append(stripped)
            continue
        if _looks_like_person_name_token(t):
            attendees.append(stripped if stripped else t)
        else:
            topics.append(t)
    return topics, attendees


def parse_filename(
    filename: str,
    known_people: set[str] | None = None,
) -> dict[str, Any]:
    """ファイル名を構造化パースする。

    Args:
        filename: ファイル名（拡張子含むOK）
        known_people: ナレッジから取得した既知の人名集合（参加者判定に使用）

    Returns:
        {
            "raw_stem":          str,
            "date_raw":          str | None,   # 元のトークン（"20260411"）
            "date":              str | None,   # 正規化済み（"2026-04-11"）
            "meeting_scope":     "external" | "internal" | "unknown",
            "customer":          str | None,   # external のときの顧客名
            "topics":            list[str],    # 会議内容を表すトークン
            "attendees":         list[str],    # 参加者と推定されるトークン
            "raw_tokens":        list[str],    # 元の分割結果（参考）
        }
    """
    known_people = known_people or set()
    stem = Path(filename).stem
    cleaned = _BRACKETED_RE.sub("", stem)
    tokens = [t.strip() for t in _SPLIT_RE.split(cleaned) if t.strip()]

    result: dict[str, Any] = {
        "raw_stem": stem,
        "date_raw": None,
        "date": None,
        "meeting_scope": "unknown",
        "customer": None,
        "topics": [],
        "attendees": [],
        "raw_tokens": list(tokens),
    }

    if not tokens:
        return result

    # 1) 日付候補（YYYY_MMDD / YYYYMMDD / YYMMDD / MMDD いずれも対応）
    date_info, consumed = _try_consume_date_tokens(tokens)
    result["date_raw"] = date_info["date_raw"]
    result["date"] = date_info["date"]
    idx = consumed

    # 2) 顧客名 or プレセナ判定
    if idx < len(tokens):
        second = tokens[idx]
        if _is_internal_marker(second):
            result["meeting_scope"] = "internal"
            result["customer"] = None
        else:
            result["meeting_scope"] = "external"
            result["customer"] = second
        idx += 1

    # 3) 会議内容＋参加者
    remaining = tokens[idx:]
    topics, attendees = _split_attendees_from_topic(remaining, known_people)
    result["topics"] = topics
    result["attendees"] = attendees

    return result


def format_parsed_for_prompt(parsed: dict[str, Any]) -> str:
    """構造化パース結果を AI プロンプト用に整形する。

    AI 補正/検出/質問生成プロンプトに注入することで、AIが
    「顧客企業は誰か」「参加者は誰か」を文脈として理解できるようにする。
    """
    if not parsed:
        return ""
    lines: list[str] = []
    if parsed.get("date"):
        lines.append(f"開催日: {parsed['date']}")
    scope = parsed.get("meeting_scope")
    if scope == "internal":
        lines.append("会議区分: 社内会議（プレセナ・ストラテジック・パートナーズ内部）")
    elif scope == "external":
        customer = parsed.get("customer") or "?"
        lines.append(f"会議区分: 外部会議（顧客企業: {customer}）")
    topics = parsed.get("topics") or []
    if topics:
        lines.append(f"会議内容（推定）: {'、'.join(topics)}")
    attendees = parsed.get("attendees") or []
    if attendees:
        lines.append(f"参加者（推定）: {'、'.join(attendees)}")
    if not lines:
        return ""
    body = "\n".join(f"- {ln}" for ln in lines)
    return (
        "\n\n【ファイル名から抽出した会議メタ情報】\n"
        f"{body}\n"
        "上記情報を文脈理解の前提として活用してください。"
        "音声認識で顧客名・参加者名が誤変換されている場合は、上記の正しい表記に修正してください。"
    )


def extract_known_people_from_knowledge(memos: list[str]) -> set[str]:
    """ナレッジから既知の人名集合を抽出する（参加者判定用）。

    厳密化ロジック（誤検出防止のため）:
    - 「（ひらがな読み）」括弧を持つ行のみを人名候補として扱う
        例: 「相原隆太郎（あいはらりゅうたろう）＝...」  → 採用
            「仕事力サーベイ＝...」                       → 不採用（人名ではない）
    - または「○○さん」「○○氏」のように人名サフィックスが付いている行
    - 苗字（漢字2-3字）も自動登録（ファイル名では苗字単体表記が多いため）
    """
    out: set[str] = set()
    # 人名候補と判断するパターン1: 「先頭の漢字/カナ語 + （ひらがな読み）」
    pat_with_reading = re.compile(
        r"^([A-Za-z一-龠々ァ-ヴー・]{2,12})（([ぁ-んー]{2,20})）"
    )
    # 人名候補と判断するパターン2: 「先頭の漢字/カナ語 + さん/氏/様」
    pat_with_honorific = re.compile(
        r"^([一-龠々ァ-ヴー・]{2,8})(?:さん|氏|様)[＝=・\s]"
    )

    def _register_name(name: str) -> None:
        if not name:
            return
        out.add(name)
        # フルネームから苗字を抽出: 漢字のみで4文字以上 → 先頭2文字を苗字候補に
        if re.fullmatch(r"[一-龠々]{4,}", name):
            out.add(name[:2])
            # 「長谷川」のような3文字苗字も拾う
            out.add(name[:3])
        elif re.fullmatch(r"[一-龠々]{3}", name):
            # 3文字漢字名: 先頭2文字（苗字）として登録
            out.add(name[:2])

    for memo in memos or []:
        s = str(memo or "").strip()
        if not s:
            continue
        m1 = pat_with_reading.match(s)
        if m1:
            _register_name(m1.group(1).strip())
            continue
        m2 = pat_with_honorific.match(s)
        if m2:
            _register_name(m2.group(1).strip())
            continue
    return out
