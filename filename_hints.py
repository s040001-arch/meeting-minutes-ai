import re
from pathlib import Path

_BRACKETED_SEGMENT_RE = re.compile(r"[【\[\(（][^】\]\)）]*[】\]\)）]")
_SPLIT_RE = re.compile(r"[_\-\s\u3000・/]+")
_DATE_TOKEN_RE = re.compile(r"^\d{4,8}$")

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


def extract_filename_hints(filename: str) -> list[str]:
    stem = Path(filename).stem
    cleaned = _BRACKETED_SEGMENT_RE.sub("", stem)
    tokens = _SPLIT_RE.split(cleaned)

    results: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        token = token.strip()
        if len(token) <= 1:
            continue
        if _DATE_TOKEN_RE.fullmatch(token):
            continue
        if token.casefold() in _GENERIC_TOKENS:
            continue
        if token in seen:
            continue
        seen.add(token)
        results.append(token)
    return results


def format_hints_for_prompt(hints: list[str]) -> str:
    if not hints:
        return ""
    joined = ", ".join(hints)
    return (
        "\n\n【重要コンテキスト】この会議では以下の固有名詞・キーワードが登場します:\n"
        f"{joined}\n"
        "音声認識の誤りでこれらが別の表記（例: 似た発音の別単語）になっている場合は、"
        "上記の正しい表記に修正してください。\n"
        "例: 「CHR」→「THR」、「サービス」→「サーベイ」など。"
    )


def format_filename_meta_for_prompt(parsed: dict | None) -> str:
    """ファイル名構造化パース結果をプロンプトに整形する。

    filename_parser.parse_filename() の戻り値を入力に取り、
    顧客企業・参加者・会議内容を明示してAIに渡す。
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
        lines.append(f"参加者（推定・苗字のみの場合あり）: {'、'.join(attendees)}")
    if not lines:
        return ""
    body = "\n".join(f"- {ln}" for ln in lines)
    return (
        "\n\n【ファイル名から抽出した会議メタ情報】\n"
        f"{body}\n"
        "上記情報を文脈理解の前提として活用してください。"
        "音声認識で顧客名・参加者名が誤変換されている場合は、上記の正しい表記に修正してください。"
        "外部会議の場合、参加者は『顧客企業側』と『プレセナ側（弊社）』が混在することに注意してください。"
    )
