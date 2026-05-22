import argparse
import json
import os
import re


# Mechanical correction (Task 4-2):
# - readability improvement without meaning change
# - limited, deterministic filler/newline handling for AI-main pipeline
#
# IMPORTANT (per user spec):
# - REMOVE targets: えー / あー / うーん / お疲れ様です / なんか / ええ / えっと
# - NEVER remove: はい / ま
FILLERS_TO_REMOVE = ["えー", "あー", "うーん", "お疲れ様です", "なんか", "ええ", "えっと"]
# 相槌語: 単体では残すが、連続出現（3回以上）は異常パターンとして圧縮・除去する
AIZUCHI_WORDS = ["うん", "はい", "ええ", "ああ"]
DEFAULT_CORRECTION_DICT_PATH = os.path.join("data", "correction_dict.json")
PARTICLE_PAIR_REPLACEMENTS = {
    "をに": "に",
    "がが": "が",
    "をを": "を",
    "にに": "に",
    "でで": "で",
    "とと": "と",
    "のの": "の",
}
COMMON_NOISE_REPLACEMENTS = {
    "ご質問をに": "ご質問に",
    "をにお答え": "にお答え",
    "何かえっと": "えっと",
    "なんかえっと": "えっと",
    "お願いしま す": "お願いします",
    "係数詐欺": "具体的",
    "係数詐欺で": "具体的に",
    "集合形成": "集合研修",
    "集合形成の": "集合研修の",
    "パワー文句金": "月金",
    "禁止受けても": "診断受けても",
    "20年のがある": "20年ものがある",
    "アウルの呼吸": "阿吽の呼吸",
    "アウンの呼吸": "阿吽の呼吸",
    "本店にあります": "本当にあります",
    "本店にある": "本当にある",
    "本店に重要": "本当に重要",
}
REPEATED_SHORT_PHRASES = [
    "ありがとうございます",
    "ありがとうございました",
    "お願いします",
    "すいません",
    "すみません",
    "はい",
]


def normalize_punctuation(text: str) -> str:
    # Basic punctuation unification for stable matching.
    s = text
    s = s.replace(",", "、").replace(".", "。")
    s = s.replace("，", "、").replace("．", "。")
    s = s.replace("！", "!").replace("？", "?")
    return s


def normalize_newlines_only(text: str) -> str:
    # Keep newline structure temporarily for line/sentence-start rules.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_spaces_and_newlines(text: str) -> str:
    """
    目的（ユーザー指定）:
    - 単一改行（\\n）はスペースに変換
    - 連続改行（\\n\\n 以上）は段落として維持
    """
    s = normalize_newlines_only(text)
    # 段落区切りは \n\n に圧縮（3つ以上も含める）
    s = re.sub(r"\n{3,}", "\n\n", s)
    # \n\n を一時プレースホルダへ
    para = "\u0000PARA\u0000"
    s = s.replace("\n\n", para)
    # 残りの単一改行をスペースへ
    s = s.replace("\n", " ")
    # 段落へ戻す
    s = s.replace(para, "\n\n")
    # 空白圧縮
    s = re.sub(r"[ \t\u3000]+", " ", s)
    # 段落境界まわりの空白を削る
    s = re.sub(r" *\n\n *", "\n\n", s)
    return s.strip()


def _remove_standalone_line(text: str, filler: str) -> str:
    """
    単独発話（その語だけの行）を削除。
    例: "えー" -> 削除
    """
    esc = re.escape(filler)
    # 行頭/行末のみのマッチ（空白 + filler + 任意の句読点のみ）
    pattern = rf"(?m)^[ \t\u3000]*{esc}[ \t\u3000]*[、。]?[ \t\u3000]*$"
    return re.sub(pattern, "", text)


def _remove_sentence_start(text: str, filler: str) -> str:
    """
    文頭に出る場合を削除。
    例: "えー、今回の件ですが" -> 削除
    """
    esc = re.escape(filler)
    # 行頭（改行直後）で filler が始まるケース
    # filler +（空白）+（、，）を丸ごと削り、次の文を先頭にする。
    pattern_line_start = rf"(?m)^[ \t\u3000]*{esc}[ \t\u3000]*(?:[、，])?"
    text = re.sub(pattern_line_start, "", text)

    # 文の終端句点（。！？）直後で filler が始まるケース
    pattern_after_punct = rf"(?<=[。！？!?])\s*{esc}[ \t\u3000]*(?:[、，])?"
    text = re.sub(pattern_after_punct, "", text)
    return text


def _compress_or_delete_consecutive_fillers(text: str, filler: str) -> str:
    """
    同一語の連続出現（2回以上）を処理。
    - 2回連続: 1回に圧縮（例: えーえー -> えー）
    - 3回以上: 削除（例: えーえーえー -> 削除）

    連続判定は:
    - カンマ・句点あり（「えー、えー」）
    - スペースあり
    - 改行あり
    """
    esc = re.escape(filler)
    # filler と filler の間に許す“区切り”を広めに（指定に沿って必要要素のみ）
    # - whitespace（スペース/タブ/全角空白/改行）
    # - comma / period（、。）
    sep = r"[ \t\u3000\r\n]*[、。,.!?！？]*[ \t\u3000\r\n]*"
    # 連続（2回以上）を1つのマッチへ
    pattern = re.compile(rf"{esc}(?:{sep}{esc})+")

    def repl(m: re.Match) -> str:
        chunk = m.group(0)
        count = chunk.count(filler)
        if count == 2:
            return filler
        if count >= 3:
            return ""
        return chunk

    return pattern.sub(repl, text)


def compress_consecutive_aizuchi(text: str) -> str:
    """
    相槌語（うん、はい等）の異常な連続出現を圧縮・除去する。
    - 2回連続: 1回に圧縮
    - 3回以上連続: 完全に除去
    単体の出現は残す（AI補正で文脈判断させる）。
    """
    s = text
    for word in AIZUCHI_WORDS:
        esc = re.escape(word)
        sep = r"[ \t\u3000\r\n]*[、。,.!?！？]*[ \t\u3000\r\n]*"
        pattern = re.compile(rf"{esc}(?:{sep}{esc})+")

        def _repl(m: re.Match, w: str = word) -> str:
            count = m.group(0).count(w)
            if count >= 3:
                return ""
            return w

        s = pattern.sub(_repl, s)
    return s


def remove_sentence_start_fillers(text: str) -> str:
    """
    文頭・句点直後に現れる単独フィラー的な「あ」「あっ」を除去する。
    例: 「あ、そうですね」→「そうですね」
        「あっ、本当ですか」→「本当ですか」
    ただし「ああ」「あの」等は対象外。
    """
    s = text
    s = re.sub(r"(?m)^[ \t\u3000]*あっ?[、，]\s*", "", s)
    s = re.sub(r"(?<=[。！？!?])\s*あっ?[、，]\s*", "", s)
    return s


def remove_fillers(text: str) -> str:
    """
    フィラー削除ルール（ユーザー指定、重要）:
    - 対象語のみを扱う
    - はい/ま は削除対象から除外
    - 意味は変えない
    - 過剰削除しない（軽い整形）
    """
    s = normalize_newlines_only(text)
    s = normalize_punctuation(s)

    # 1) 単独発話（その語だけの行）を削除
    for filler in FILLERS_TO_REMOVE:
        s = _remove_standalone_line(s, filler)

    # 2) 文頭に出るフィラーを削除（文の先頭をきれいにする）
    for filler in FILLERS_TO_REMOVE:
        s = _remove_sentence_start(s, filler)

    # 3) 同一語の連続出現を圧縮/削除
    for filler in FILLERS_TO_REMOVE:
        s = _compress_or_delete_consecutive_fillers(s, filler)

    # 4) 削除後に文頭が「、」だけになるケースを軽く整える
    #    （えーえーえー、のような形を想定）
    s = re.sub(r"(?m)^[ \t\u3000]*[、，]\s*", "", s)

    return s


def load_correction_dict(path: str) -> dict[str, str]:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in data.items():
            kk = str(k).strip()
            vv = str(v).strip()
            if kk and vv:
                out[kk] = vv
        return out
    except (OSError, json.JSONDecodeError):
        return {}


def apply_dictionary_replacements(text: str, replacements: dict[str, str]) -> str:
    s = text
    # Apply longest keys first so shorter keys cannot accidentally match inside
    # a longer key that has already been (or will be) corrected.
    # Example: if both "HR"→"A" and "THR"→"B" existed, applying "HR" first would
    # turn "THR" into "TA", which then wouldn't match "THR"→"B".
    for wrong in sorted(replacements, key=len, reverse=True):
        correct = replacements[wrong]
        s = s.replace(wrong, correct)
    return s


def cleanup_common_noise(text: str) -> str:
    s = text
    for wrong, correct in COMMON_NOISE_REPLACEMENTS.items():
        s = s.replace(wrong, correct)
    for wrong, correct in PARTICLE_PAIR_REPLACEMENTS.items():
        s = s.replace(wrong, correct)
    s = re.sub(r"[。．\.]{2,}", "。", s)
    s = re.sub(r"[、,]{2,}", "、", s)
    s = re.sub(r"([!?！？]){2,}", r"\1", s)
    s = re.sub(r"([。!?！？]\s*)(はい|すいません|すみません)([。!?！？]\s*)\2([。!?！？])", r"\1\2\4", s)
    return s


def compress_repeated_short_phrases(text: str) -> str:
    s = text
    for phrase in REPEATED_SHORT_PHRASES:
        esc = re.escape(phrase)
        pattern = rf"(?:{esc}[、。!?！？\s]*){{2,}}"
        s = re.sub(pattern, f"{phrase}。", s)
    return s


# 会議前後の雑音として除去しやすい短い冒頭・末尾パターン
_EDGE_START_NOISE_RE = re.compile(
    r"^(?:カメラが(?:奥に)?[。]?|映像(?:が)?(?:映)?(?:って)?(?:ます)?[。]?)$"
)
_EDGE_END_NOISE_RES = (
    re.compile(r"^何でもない(?:です)?[。]?$"),
    re.compile(r"^なんかこんな感じでしょうね[。]?$"),
    re.compile(r"^うん、?でもなんかすごい[。]?$"),
    re.compile(r"^ポジティブに私受け止めたのは[？?]?$"),
    re.compile(r"^お疲れ様でした[。]?$"),
)


def trim_edge_noise(text: str) -> str:
    """会議前後に混入しやすい短い雑音行を除去する。

    - 冒頭: 「カメラが奥に。」等の機材調整コメント
    - 末尾: 会議終了後の振り返り雑談（「何でもないです」等）
    """
    lines = text.splitlines()
    if not lines:
        return text

    # 冒頭ノイズ（最大2行まで）
    for _ in range(min(2, len(lines))):
        first = lines[0].strip()
        if first and _EDGE_START_NOISE_RE.match(first):
            lines.pop(0)
        else:
            break

    # 末尾ノイズ: 会議の締め（失礼いたします/ありがとうございました）以降の短い雑談
    close_idx = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if "失礼いたします" in s or "ありがとうございました" in s:
            close_idx = i

    if close_idx >= 0 and close_idx < len(lines) - 1:
        tail = lines[close_idx + 1 :]
        trimmed_tail: list[str] = []
        for line in tail:
            s = line.strip()
            if not s:
                continue
            if any(p.match(s) for p in _EDGE_END_NOISE_RES):
                continue
            if len(s) <= 25 and ("こんな感じ" in s or "受け止め" in s):
                continue
            trimmed_tail.append(line)
        lines = lines[: close_idx + 1] + trimmed_tail

    return "\n".join(lines).strip()


def apply_mechanical_corrections(
    text: str,
    correction_dict_path: str = DEFAULT_CORRECTION_DICT_PATH,
) -> str:
    replacements = load_correction_dict(correction_dict_path)
    s = apply_dictionary_replacements(text, replacements)
    s = cleanup_common_noise(s)
    # Filler rules first (need line/sentence-start visibility)
    s = remove_fillers(s)
    s = compress_consecutive_aizuchi(s)
    s = remove_sentence_start_fillers(s)
    s = compress_repeated_short_phrases(s)
    s = cleanup_common_noise(s)
    s = trim_edge_noise(s)
    # Newline normalization last (user-specified output format)
    s = normalize_spaces_and_newlines(s)
    return s


def main() -> None:
    parser = argparse.ArgumentParser(
        description="機械補正（改行/フィラー軽整形）を適用（Task 4-2）"
    )
    parser.add_argument("--input", required=True, help="入力テキストファイル")
    parser.add_argument(
        "--output",
        default=None,
        help="出力テキストファイル（未指定時: *_mechanical.txt）",
    )
    parser.add_argument(
        "--output-encoding",
        choices=["utf-8", "utf-8-sig"],
        default="utf-8",
        help="出力文字コード（デフォルト: utf-8、utf-8-sigはBOM付き）",
    )
    parser.add_argument(
        "--correction-dict",
        default=DEFAULT_CORRECTION_DICT_PATH,
        help="補正辞書JSON（デフォルト: data/correction_dict.json）",
    )
    args = parser.parse_args()

    if not os.path.exists(args.input):
        raise FileNotFoundError(f"input file not found: {args.input}")

    with open(args.input, "r", encoding="utf-8") as f:
        original = f.read()

    replacements = load_correction_dict(args.correction_dict)
    corrected = apply_mechanical_corrections(original, correction_dict_path=args.correction_dict)

    output_path = args.output
    if not output_path:
        base, ext = os.path.splitext(args.input)
        output_path = f"{base}_mechanical{ext or '.txt'}"

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding=args.output_encoding) as f:
        f.write(corrected)

    print(f"input={args.input}")
    print(f"output={output_path}")
    print(f"output_encoding={args.output_encoding}")
    print(f"before_length={len(original)}")
    print(f"after_length={len(corrected)}")
    print(f"correction_dict={args.correction_dict}")
    print(f"correction_dict_entries={len(replacements)}")
    print(
        "rules=apply_dictionary_replacements,cleanup_common_noise,"
        "normalize_spaces_and_newlines,normalize_punctuation,remove_fillers,"
        "compress_repeated_short_phrases"
    )


if __name__ == "__main__":
    main()

