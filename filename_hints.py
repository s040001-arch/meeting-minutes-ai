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
