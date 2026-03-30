import argparse
import json
import os
import re
from typing import Dict, List, Optional, Tuple


TYPE_PRIORITY = {
    "固有名詞": 0,
    "数値": 1,
    "主語": 2,
}


def split_sentences(text: str) -> List[str]:
    parts = re.split(r"[。\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def load_unknown_points(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("unknown points file must be a JSON array.")
    return [x for x in data if isinstance(x, dict)]


def choose_one_unknown(unknown_points: List[Dict[str, str]]) -> Dict[str, str]:
    if not unknown_points:
        raise ValueError("unknown points is empty.")

    def key_func(item: Dict[str, str]) -> Tuple[int, int]:
        t = str(item.get("type", ""))
        priority = TYPE_PRIORITY.get(t, 999)
        # 同タイプ内は先頭優先（入力順）
        return (priority, 0)

    # min + enumerate で入力順を保持したまま選ぶ
    idx, selected = min(
        enumerate(unknown_points),
        key=lambda x: (TYPE_PRIORITY.get(str(x[1].get("type", "")), 999), x[0]),
    )
    _ = idx
    return selected


def find_context(
    full_text: str,
    target_text: str,
) -> Tuple[str, str]:
    """
    target_text を含む文を見つけ、前後1文を返す。
    見つからない場合は空文字を返す。
    """
    if not full_text.strip() or not target_text.strip():
        return "", ""

    sentences = split_sentences(full_text)
    for i, sentence in enumerate(sentences):
        if target_text in sentence or sentence in target_text:
            prev_sentence = sentences[i - 1] if i > 0 else ""
            next_sentence = sentences[i + 1] if i + 1 < len(sentences) else ""
            return prev_sentence, next_sentence
    return "", ""


def build_question(selected: Dict[str, str], prev_ctx: str, next_ctx: str) -> str:
    target_type = str(selected.get("type", "不明"))
    target_text = str(selected.get("text", "")).strip()
    reason = str(selected.get("reason", "情報が不足しています。")).strip()

    what_to_ask = {
        "固有名詞": "正式な名称（会社名・人名・製品名など）",
        "数値": "具体的な数値・金額・件数・期限",
        "主語": "実施主体（誰が対応するか）",
    }.get(target_type, "不足している情報")

    lines = [
        "【不明箇所の確認】",
        "",
        "1) 不明箇所の引用",
        f"- {target_text}",
        "",
        "2) 前後の文脈",
        f"- 前: {prev_ctx if prev_ctx else '(なし)'}",
        f"- 後: {next_ctx if next_ctx else '(なし)'}",
        "",
        "3) なぜ不明か",
        f"- {reason}",
        "",
        "4) 教えてほしいこと",
        f"- 上記の箇所について、{what_to_ask}を教えてください。",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="不明箇所リストから優先順位で1件選び、質問文を生成（Task 5-1）"
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--unknowns",
        default=None,
        help="不明箇所リストJSONのパス（未指定時: data/transcriptions/{job_id}/unknown_points.json）",
    )
    parser.add_argument(
        "--text",
        default=None,
        help=(
            "前後文脈・選定スコア用の本文（任意）。未指定時は job 内の "
            "ai_corrected / merged_transcript_ai / merged_transcript_after_qa を順に探す。"
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Task 5-1結果JSONの出力先（未指定時: "
            "data/transcriptions/{job_id}/question_result.json）"
        ),
    )
    args = parser.parse_args()

    unknowns_path = args.unknowns or os.path.join(
        "data", "transcriptions", args.job_id, "unknown_points.json"
    )
    if not os.path.exists(unknowns_path):
        raise FileNotFoundError(f"unknowns file not found: {unknowns_path}")

    unknown_points = load_unknown_points(unknowns_path)
    output_path = args.output or os.path.join(
        "data", "transcriptions", args.job_id, "question_result.json"
    )
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    if not unknown_points:
        result_payload = {
            "job_id": args.job_id,
            "question_status": "none",
            "message": "不明箇所は0件のため、質問は生成しません。",
            "selected_unknown": None,
            "question_text": "",
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result_payload, f, ensure_ascii=False, indent=2)
        print(f"job_id={args.job_id}")
        print(f"unknowns={unknowns_path}")
        print(f"saved_result={output_path}")
        print("question_status=none")
        print("message=不明箇所は0件のため、質問は生成しません。")
        return

    job_trans_dir = os.path.join("data", "transcriptions", args.job_id)
    text_path = args.text
    if not text_path:
        for name in ("ai_corrected.txt", "merged_transcript_ai.txt", "merged_transcript_after_qa.txt"):
            p = os.path.join(job_trans_dir, name)
            if os.path.isfile(p):
                text_path = p
                break
        if not text_path:
            text_path = os.path.join(job_trans_dir, "merged_transcript.txt")
    full_text = ""
    if text_path and os.path.isfile(text_path):
        with open(text_path, "r", encoding="utf-8") as f:
            full_text = f.read()

    selection_audit = None
    if full_text.strip():
        from question_value_selection import pop_value_fields, select_one_unknown_value_based

        selected, selection_audit = select_one_unknown_value_based(unknown_points, full_text)
        pop_value_fields(selected)
    else:
        selected = choose_one_unknown(unknown_points)

    prev_ctx, next_ctx = find_context(full_text, str(selected.get("text", "")))
    question = build_question(selected, prev_ctx, next_ctx)
    result_payload = {
        "job_id": args.job_id,
        "question_status": "generated",
        "message": "",
        "selected_unknown": selected,
        "question_text": question,
    }
    if selection_audit is not None:
        result_payload["selection_audit"] = selection_audit
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result_payload, f, ensure_ascii=False, indent=2)

    print(f"job_id={args.job_id}")
    print(f"unknowns={unknowns_path}")
    print(f"context_text={text_path if full_text else '(not found / not used)'}")
    print(f"saved_result={output_path}")
    print("selected_unknown=")
    print(json.dumps(selected, ensure_ascii=False, indent=2))
    print("")
    print("generated_question=")
    print(question)


if __name__ == "__main__":
    main()

