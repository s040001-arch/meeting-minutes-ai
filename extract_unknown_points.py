import argparse
import json
import os
import re
from typing import Dict, List


def split_sentences(text: str) -> List[str]:
    # MVP向けの簡易分割。句点や改行で区切る。
    parts = re.split(r"[。\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def detect_ambiguous_named_entity(sentence: str) -> bool:
    patterns = [
        r"\bA社\b",
        r"\bB社\b",
        r"\b某社\b",
        r"〇〇",
        r"XX",
    ]
    return any(re.search(p, sentence) for p in patterns)


def detect_ambiguous_number_or_deadline(sentence: str) -> bool:
    patterns = [
        r"いくらか",
        r"金額未定",
        r"今月中",
        r"早めに",
        r"数件",
        r"何件か",
        r"後で調整",
    ]
    return any(re.search(p, sentence) for p in patterns)


def detect_missing_subject(sentence: str) -> bool:
    action_patterns = [
        r"(対応|更新|共有|送付|提出|連絡|確認|実施)する",
        r"(対応|更新|共有|送付|提出|連絡|確認|実施)します",
    ]
    has_action = any(re.search(p, sentence) for p in action_patterns)
    has_owner_hint = bool(re.search(r"(さん|氏|担当|チーム|部|課|私|弊社|当社)", sentence))
    return has_action and not has_owner_hint


def extract_unknown_points(text: str) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    for sentence in split_sentences(text):
        if detect_ambiguous_named_entity(sentence):
            results.append(
                {
                    "type": "固有名詞",
                    "text": sentence,
                    "reason": "固有名詞があいまいで、対象を一意に特定できません。",
                }
            )
        if detect_ambiguous_number_or_deadline(sentence):
            results.append(
                {
                    "type": "数値",
                    "text": sentence,
                    "reason": "金額・件数・期日などの定量情報が不明確です。",
                }
            )
        if detect_missing_subject(sentence):
            results.append(
                {
                    "type": "主語",
                    "text": sentence,
                    "reason": "実施主体（誰がやるか）が明示されていません。",
                }
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI補正後テキストから不明箇所を抽出（Task 4-4）"
    )
    parser.add_argument("--input", required=True, help="AI補正後テキストファイル")
    parser.add_argument(
        "--output",
        default=None,
        help="不明箇所リストJSONの出力先（未指定時: 入力と同じディレクトリに unknown_points.json）",
    )
    args = parser.parse_args()

    if not os.path.exists(args.input):
        raise FileNotFoundError(f"input file not found: {args.input}")

    with open(args.input, "r", encoding="utf-8") as f:
        text = f.read()

    unknown_points = extract_unknown_points(text)

    output_path = args.output or os.path.join(
        os.path.dirname(args.input),
        "unknown_points.json",
    )
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(unknown_points, f, ensure_ascii=False, indent=2)

    # リスト形式で出力（MVP: print + json保存）
    print(f"saved_unknowns={output_path}")
    print(json.dumps(unknown_points, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

