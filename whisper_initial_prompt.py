"""
Whisper の initial_prompt を構築するモジュール。

faster-whisper の initial_prompt は語彙バイアス用で、長すぎると先頭以外は無視される。
（仕様上 ~224 トークン = およそ日本語 200〜250文字程度を上限と考える）

ここではナレッジシートの自由記述メモ群から、人名・社名・固有名詞・社内用語を
抽出し、Whisper が認識しやすい「自然な文」として組み立てる。
"""
from __future__ import annotations

import re
from typing import Iterable

# initial_prompt の最大文字数（保守的な値: 日本語1文字≒1〜2トークン想定で 230 字以内）
_MAX_PROMPT_CHARS = 230

# ナレッジから固有名詞らしき部分を抜くための正規表現
# 「Xxx（よみ）」「XXX」「X氏」など、先頭の表記＋カッコ内の読み仮名を主に拾う
_TERM_HEAD_RE = re.compile(
    r"^([A-Za-zＡ-Ｚａ-ｚ一-龠々ぁ-んァ-ヴー・]+(?:[氏さん])?(?:（[^）]+）)?)"
)

# 文字列中から英大文字略称（NRE, THR, etc.）と漢字＋カナの固有名詞を補助的に抽出
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}\b")
_PROPER_NOUN_RE = re.compile(r"[一-龠々]{2,4}(?:さん|氏)?")


def _extract_term_from_memo(memo: str) -> str | None:
    """ナレッジ1行から「先頭の用語表記」を抽出する。

    例:
      「相原隆太郎（あいはらりゅうたろう）＝プレセナ・ストラテジック・パートナーズの...」
        → 「相原隆太郎」
      「NRE＝野村不動産（Nomura Real Estate Development Co., Ltd.）の略称」
        → 「NRE」
      「楠原さん（くすはらさん）＝プレセナ社員...」
        → 「楠原さん」
    """
    s = memo.strip()
    if not s:
        return None
    head = s.split("＝", 1)[0].split("=", 1)[0].strip()
    if not head:
        return None
    m = _TERM_HEAD_RE.match(head)
    if not m:
        return head[:20]
    term = m.group(1).strip()
    # 末尾の括弧（よみ）は外して用語本体だけ残す
    paren_idx = term.find("（")
    if paren_idx > 0:
        term = term[:paren_idx].strip()
    return term or None


def _is_useful_term(term: str) -> bool:
    if not term or len(term) < 2:
        return False
    # ひらがなのみ、または一般語っぽいものは除外
    generic = {
        "会社", "営業", "研修", "業務", "資料", "提案", "顧客", "お客",
        "事業", "案件", "サービス", "プロジェクト", "ミーティング", "議事録",
    }
    if term in generic:
        return False
    return True


def _dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not item:
            continue
        key = item.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def build_initial_prompt(
    *,
    knowledge_memos: list[str] | None = None,
    filename_hints: list[str] | None = None,
    extra_terms: list[str] | None = None,
    job_context: dict | None = None,
    parsed_filename: dict | None = None,
    max_chars: int = _MAX_PROMPT_CHARS,
) -> str:
    """Whisper 用 initial_prompt を構築する。

    優先度（高い順、Whisper の語彙バイアスは前半ほど効きが強い）:
      1) parsed_filename（その会議の顧客・参加者・トピック）← 最重要
      2) filename_hints（後方互換: 旧形式のヒント語）
      3) job_context の attendees / company / project 等
      4) knowledge_memos 由来の用語（汎用ナレッジ）

    Whisper の initial_prompt は「自然な日本語の文」として与えると効果が高いため、
    最終的に「以下の語が登場します: A, B, C ... 。」の形に組み立てる。
    """
    terms: list[str] = []

    # 1) parsed_filename（最優先）— 顧客名→参加者→トピックの順
    if isinstance(parsed_filename, dict):
        customer = parsed_filename.get("customer")
        if isinstance(customer, str) and _is_useful_term(customer.strip()):
            terms.append(customer.strip())
        for a in parsed_filename.get("attendees") or []:
            if isinstance(a, str) and _is_useful_term(a.strip()):
                terms.append(a.strip())
        for t in parsed_filename.get("topics") or []:
            if isinstance(t, str) and _is_useful_term(t.strip()):
                terms.append(t.strip())

    # 2) filename_hints（後方互換）
    for h in filename_hints or []:
        h = h.strip()
        if _is_useful_term(h):
            terms.append(h)

    # 3) job_context から拾える固有名詞（attendees, company, etc.）
    if isinstance(job_context, dict):
        for key in ("attendees", "company", "project", "client", "title"):
            val = job_context.get(key)
            if isinstance(val, str):
                for token in re.split(r"[,、，\s/]+", val):
                    token = token.strip()
                    if _is_useful_term(token):
                        terms.append(token)
            elif isinstance(val, list):
                for token in val:
                    if isinstance(token, str) and _is_useful_term(token.strip()):
                        terms.append(token.strip())

    # 4) ナレッジから用語抽出
    for memo in knowledge_memos or []:
        term = _extract_term_from_memo(memo)
        if term and _is_useful_term(term):
            terms.append(term)

    # 5) 明示的な追加語
    for t in extra_terms or []:
        if _is_useful_term(t):
            terms.append(t)

    terms = _dedupe_preserve_order(terms)
    if not terms:
        return ""

    # ヘッダー文を組み立て、上限文字数に収まるよう用語をトリミング
    header = "この会議では次の固有名詞・人名・社内用語が登場します: "
    suffix = "。"
    available = max(50, max_chars - len(header) - len(suffix))

    chosen: list[str] = []
    used = 0
    for term in terms:
        addition = (len(term) + 2) if chosen else len(term)  # ", " を加味
        if used + addition > available:
            break
        chosen.append(term)
        used += addition

    if not chosen:
        return ""

    return f"{header}{'、'.join(chosen)}{suffix}"


def build_initial_prompt_from_files(
    *,
    knowledge_memos: list[str] | None,
    filename: str | None,
    job_context: dict | None,
) -> str:
    """ファイル名・コンテキストから initial_prompt を作るショートカット。"""
    hints: list[str] = []
    if filename:
        try:
            from filename_hints import extract_filename_hints
            hints = extract_filename_hints(filename)
        except Exception:
            hints = []

    return build_initial_prompt(
        knowledge_memos=knowledge_memos,
        filename_hints=hints,
        job_context=job_context,
    )
