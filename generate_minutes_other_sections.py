import argparse
import os
from typing import Tuple


def extract_title_and_transcript(minutes_draft_text: str) -> Tuple[str, str]:
    lines = minutes_draft_text.splitlines()
    title = "minutes"
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip() or title
            break

    # `## 発言録` 以降を発言録部分として保持する（入力フォーマット揺れに備える）
    transcript_lines = []
    in_transcript = False
    for line in lines:
        if not in_transcript:
            if line.strip() in {"## 発言録", "##発言録"}:
                in_transcript = True
            continue
        transcript_lines.append(line)

    transcript = "\n".join(transcript_lines).strip()
    if not transcript:
        raise ValueError("minutes draft does not contain transcript section: `## 発言録`.")
    return title, transcript


def build_minutes_structured_md(title: str, transcript_md: str) -> str:
    # Task 6-2 は「構造ができる」こと優先。内容（要約等）は後段の精度改善で差し替える想定。
    return (
        f"# {title}\n\n"
        "## 発言録\n\n"
        f"{transcript_md}\n\n"
        "## その他項目\n\n"
        "### 参加者\n"
        "- （未設定）\n\n"
        "### 会議概要\n"
        "- （未設定）\n\n"
        "### 決まったこと\n"
        "- （未設定）\n\n"
        "### 残論点\n"
        "- （未設定）\n\n"
        "### Next Action\n"
        "- （未設定）\n\n"
        "### 発言録ルール（重要）\n"
        "- フィラー削除、明らかな誤字修正、文の区切り整理のみ許可\n"
        "- 意味の補完や推測による書き換え、要約生成は禁止\n"
    )


def resolve_input_path(job_id: str, input_path: str | None, input_root: str) -> str:
    if input_path:
        return input_path
    job_dir = os.path.join(input_root, job_id)
    for name in ("minutes_draft.md", "minutes_draft.txt"):
        p = os.path.join(job_dir, name)
        if os.path.isfile(p):
            return p
    return os.path.join(job_dir, "minutes_draft.md")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Task 6-2: 発言録ドラフトから他セクションを付けて構造化Markdown生成"
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input",
        default=None,
        help="入力（未指定時: {input_root}/{job_id}/minutes_draft.md）",
    )
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="ジョブディレクトリのルート（デフォルト: data/transcriptions）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="出力先（未指定時: {input_root}/{job_id}/minutes_structured.md）",
    )
    args = parser.parse_args()

    in_path = resolve_input_path(args.job_id, args.input, args.input_root)
    if not os.path.isfile(in_path):
        raise FileNotFoundError(f"input file not found: {in_path}")

    with open(in_path, "r", encoding="utf-8") as f:
        draft_text = f.read()

    title, transcript_md = extract_title_and_transcript(draft_text)
    output_text = build_minutes_structured_md(title=title, transcript_md=transcript_md)

    out_path = args.output or os.path.join(
        args.input_root, args.job_id, "minutes_structured.md"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output_text)

    print(f"job_id={args.job_id}")
    print(f"input={in_path}")
    print(f"output={out_path}")
    print("status=minutes_structured_generated")


if __name__ == "__main__":
    main()

