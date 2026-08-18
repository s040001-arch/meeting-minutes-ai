import argparse
import json
import os
from pathlib import Path

from meeting_profile import load_meeting_profile, resolve_display_title
from minutes_quality_gate import run_minutes_quality_gate
from minutes_transcript_sections import generate_sectioned_transcript
from readable_transcript import resolve_minutes_transcript_text_with_stats
from transcript_paths import resolve_transcript_path_for_minutes
from transcript_section_summarizer import INTEGRATED_MIN_INPUT_CHARS

READABLE_PARTIAL_NOTICE = "※一部区間は整文を適用できませんでした（逐語のまま掲載しています）"


def _single_pass_primary_enabled() -> bool:
    raw = os.environ.get(
        "SINGLE_PASS_TRANSCRIPT_ENABLED", ""
    ).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _generate_single_pass_final(
    job_dir: str,
) -> tuple[str, str, bool, dict]:
    """Regenerate the final transcript from raw + this job's human answers."""
    from detect_unknown_points import extract_single_pass_uncertainties
    from shadow_single_pass_editor import edit_transcript_once
    from single_pass_independent_verifier import (
        verify_and_repair_until_stable,
        write_verifier_report,
    )

    job_path = Path(job_dir)
    raw_path = job_path / "merged_transcript.txt"
    raw_text = raw_path.read_text(encoding="utf-8")
    edited, editor_meta = edit_transcript_once(
        raw_text,
        job_dir=job_path,
        include_job_answers=True,
    )
    if not editor_meta.get("complete"):
        raise RuntimeError(
            "single-pass final editor output incomplete: "
            + json.dumps(editor_meta, ensure_ascii=False)
        )

    repaired, final_report, repairs = (
        verify_and_repair_until_stable(
            raw_text=raw_text,
            edited_text=edited,
            job_dir=job_path,
        )
    )
    write_verifier_report(job_path, final_report)

    # Preserve the regenerated full-context result as the canonical,
    # answer-aware transcript consumed by the existing question/Docs flow.
    after_qa_path = job_path / "merged_transcript_after_qa.txt"
    after_qa_path.write_text(repaired + "\n", encoding="utf-8")

    findings: list[dict] = []
    for marker in extract_single_pass_uncertainties(repaired):
        findings.append(
            {
                "type": "fragment",
                "quote": str(marker.get("span_text") or ""),
                "issue": str(marker.get("reason") or "意味不明"),
                "fix": "",
                "confidence": "high",
                "source": "single_pass_editor",
            }
        )
    for finding in final_report.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("severity") or "").lower() != "blocker":
            continue
        findings.append(
            {
                # Verifier blockers must reach the existing fail-closed gate.
                "type": "contradiction",
                "quote": str(
                    finding.get("edited_quote")
                    or finding.get("raw_quote")
                    or ""
                ),
                "issue": str(finding.get("issue") or "事実の矛盾"),
                "fix": str(finding.get("replacement") or ""),
                "confidence": "high",
                "source": "single_pass_independent_verifier",
            }
        )
    stats = {
        "enabled": True,
        "single_pass_primary": True,
        "model": editor_meta.get("model"),
        "input_chars": len(raw_text),
        "output_chars": len(repaired),
        "failed_chunk_idx": [],
        "total_chunks": 1,
        "editor_meta": editor_meta,
        "final_review": {
            "mode": "apply",
            "model": final_report.get("model"),
            "findings": findings,
            "applied": repairs,
            "error": final_report.get("error"),
        },
    }
    return repaired, str(raw_path), True, stats


def build_minutes_text(
    title: str,
    transcript_text: str,
    *,
    readable: bool = False,
    notice: str = "",
) -> str:
    section_label = "発言録（整文）" if readable else "発言録（逐語）"
    notice_block = f"{notice}\n\n" if notice.strip() else ""
    return (
        f"# {title}\n\n"
        f"{notice_block}"
        f"## {section_label}\n\n"
        f"{transcript_text.strip()}\n"
    )


def _record_readable_fallback_progress(
    *, job_id: str, input_root: str, stats: dict
) -> None:
    """整文フォールバック発生を progress.json に記録する（非致命）。"""
    try:
        from progress_tracker import update_job_progress

        failed = list(stats.get("failed_chunk_idx") or [])
        total = int(stats.get("total_chunks") or 0)
        update_job_progress(
            input_root=input_root,
            job_id=job_id,
            phase="step_6_1_minutes_draft",
            status="running",
            detail={
                "readable_fallback": True,
                "failed_chunk_idx": failed,
                "failed_chunk_count": len(failed),
                "total_chunks": total,
                "failed_ratio": round(len(failed) / total, 3) if total else 0.0,
            },
        )
    except Exception as e:  # noqa: BLE001
        print(f"readable_fallback_progress_record_failed={e!r}")


def _annotate_transcript_with_section_headings(
    transcript_text: str,
    job_dir: str,
) -> str:
    """Insert meaning-based section summaries. Skip only tiny fragments."""
    if len(transcript_text.strip()) < INTEGRATED_MIN_INPUT_CHARS:
        return transcript_text
    annotated = generate_sectioned_transcript(
        job_dir=job_dir,
        transcript_text=transcript_text,
    )
    print("transcript_section_headings=required")
    return annotated


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

    if _single_pass_primary_enabled():
        (
            transcript_text,
            minutes_source_path,
            readable_used,
            readable_stats,
        ) = _generate_single_pass_final(job_dir)
    else:
        (
            transcript_text,
            minutes_source_path,
            readable_used,
            readable_stats,
        ) = resolve_minutes_transcript_text_with_stats(
            job_dir=job_dir,
            source_text=source_text,
            source_path=in_path,
            meeting_profile=meeting_profile,
        )

    failed_chunk_idx = list(readable_stats.get("failed_chunk_idx") or [])
    if readable_used and failed_chunk_idx:
        _record_readable_fallback_progress(
            job_id=args.job_id,
            input_root=args.input_root,
            stats=readable_stats,
        )

    if readable_used:
        run_minutes_quality_gate(
            job_dir=job_dir,
            text=transcript_text,
            readable_stats=readable_stats,
        )

    # 話者交代での改行（2026-08-07 ユーザー決定: ラベルなし・改行のみ）。
    # 文字内容は不変（決定論ゲート付き）なのでゲート評価後に実施してよい。
    try:
        from speaker_turn_breaks import apply_speaker_turn_breaks

        transcript_text, stb_stats = apply_speaker_turn_breaks(transcript_text)
        print(f"speaker_turn_breaks={stb_stats}")
    except Exception as e:  # noqa: BLE001
        print(f"speaker_turn_breaks_failed={e!r}")

    # 分節サマリ見出しを差し込む。長文で見出しが付かない場合は公開しない。
    annotated = _annotate_transcript_with_section_headings(
        transcript_text, job_dir
    )
    output_text = build_minutes_text(
        title=title,
        transcript_text=annotated,
        readable=readable_used,
        notice=READABLE_PARTIAL_NOTICE if (readable_used and failed_chunk_idx) else "",
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
