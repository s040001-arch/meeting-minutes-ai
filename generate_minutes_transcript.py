import argparse
import os

from meeting_profile import load_meeting_profile, resolve_display_title
from readable_transcript import resolve_minutes_transcript_text
from transcript_paths import resolve_transcript_path_for_minutes
from transcript_section_summarizer import add_section_headings


def build_minutes_text(
    title: str,
    transcript_text: str,
    *,
    readable: bool = False,
) -> str:
    section_label = "発言録（整文）" if readable else "発言録（逐語）"
    return (
        f"# {title}\n\n"
        f"## {section_label}\n\n"
        f"{transcript_text.strip()}\n"
    )


def _add_section_headings_safe(
    transcript_text: str, meeting_profile: dict | None
) -> str:
    """発言録に分節サマリ見出しを付与する。失敗時は原文をそのまま返す(非致命)。"""
    try:
        return add_section_headings(transcript_text, meeting_profile)
    except Exception as e:
        print(f"transcript_section_headings_failed={e!r}")
        return transcript_text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Task 6-1: 補正済みテキストから発言録ドラフトを生成する"
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input",
        default=None,
        help="入力テキスト（未指定時: merged_transcript_after_qa.txt を優先、なければ merged_transcript.txt）",
    )
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="ジョブディレクトリのルート（デフォルト: data/transcriptions）",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="議事録タイトル（未指定時: job_id）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="出力先（未指定時: {input_root}/{job_id}/minutes_draft.md）",
    )
    args = parser.parse_args()

    in_path = resolve_transcript_path_for_minutes(args.job_id, args.input, args.input_root)
    if not os.path.isfile(in_path):
        raise FileNotFoundError(f"input file not found: {in_path}")

    with open(in_path, "r", encoding="utf-8") as f:
        source_text = f.read()
    if not source_text.strip():
        raise ValueError("input transcript is empty.")

    job_dir = os.path.join(args.input_root, args.job_id)
    meeting_profile = load_meeting_profile(job_dir)
    title = args.title or resolve_display_title(
        meeting_profile,
        job_id=args.job_id,
    )

    transcript_text, minutes_source_path, readable_used = resolve_minutes_transcript_text(
        job_dir=job_dir,
        source_text=source_text,
        source_path=in_path,
        meeting_profile=meeting_profile,
    )

    # 分節サマリ見出しを差し込み(Sonnet 数 call、~10秒)。失敗時は原文を使用。
    annotated = _add_section_headings_safe(transcript_text, meeting_profile)
    output_text = build_minutes_text(
        title=title,
        transcript_text=annotated,
        readable=readable_used,
    )

    out_path = args.output or os.path.join(
        args.input_root, args.job_id, "minutes_draft.md"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output_text)

    print(f"job_id={args.job_id}")
    print(f"input={in_path}")
    print(f"minutes_source={minutes_source_path}")
    print(f"readable_transcript_enabled={readable_used}")
    print(f"output={out_path}")
    print("status=minutes_draft_generated")


if __name__ == "__main__":
    main()
