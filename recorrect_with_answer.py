import argparse
import os

from ai_correct_text import call_openai_incorporate_answer, resolve_openai_api_key


def resolve_transcript_path(job_id: str, input_path: str | None, input_root: str) -> str:
    if input_path:
        return input_path
    job_dir = os.path.join(input_root, job_id)
    for name in ("merged_transcript_ai.txt", "merged_transcript.txt"):
        p = os.path.join(job_dir, name)
        if os.path.isfile(p):
            return p
    return os.path.join(job_dir, "merged_transcript_ai.txt")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Task 5-4（第一スライス）: 補正済み発言録に、LINE等で得た質問・回答を反映して再補正しファイルへ保存"
        )
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input",
        default=None,
        help="入力テキスト（未指定時: job 内の merged_transcript_ai.txt があればそれ、なければ merged_transcript.txt）",
    )
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="ジョブディレクトリのルート（デフォルト: data/transcriptions）",
    )
    parser.add_argument(
        "--question-text",
        required=True,
        help="ユーザーに送った確認質問の全文",
    )
    parser.add_argument(
        "--answer-text",
        required=True,
        help="ユーザーから返った回答の全文",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="出力先（未指定時: {input_root}/{job_id}/merged_transcript_after_qa.txt）",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="OpenAIモデル名（デフォルト: gpt-4o-mini）",
    )
    args = parser.parse_args()

    in_path = resolve_transcript_path(args.job_id, args.input, args.input_root)
    if not os.path.isfile(in_path):
        raise FileNotFoundError(
            f"input file not found: {in_path}\n"
            "AI補正済みを保存している場合は merged_transcript_ai.txt を置くか、--input で指定してください。"
        )

    api_key, key_source = resolve_openai_api_key()
    print(f"debug_openai_api_key_found={bool(api_key)}")
    print(f"debug_openai_api_key_source={key_source}")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. "
            "PowerShellを再起動するか、$env:OPENAI_API_KEY を現在セッションに設定してください。"
        )

    with open(in_path, "r", encoding="utf-8") as f:
        base_text = f.read()

    updated = call_openai_incorporate_answer(
        text=base_text,
        question_text=args.question_text,
        answer_text=args.answer_text,
        model=args.model,
        api_key=api_key,
    )

    out_path = args.output or os.path.join(
        args.input_root, args.job_id, "merged_transcript_after_qa.txt"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(updated)

    print(f"job_id={args.job_id}")
    print(f"input={in_path}")
    print(f"output={out_path}")
    print(f"model={args.model}")


if __name__ == "__main__":
    main()
