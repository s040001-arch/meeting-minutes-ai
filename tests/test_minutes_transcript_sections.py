from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from generate_minutes_transcript import (
    _annotate_transcript_with_section_headings,
)
from minutes_transcript_sections import (
    generate_sectioned_transcript,
    write_sectioned_transcript_artifacts,
)
from transcript_section_summarizer import (
    INTEGRATED_MIN_INPUT_CHARS,
    _assemble_sections_with_offsets,
)


class MinutesTranscriptSectionsTests(unittest.TestCase):
    @patch(
        "transcript_section_summarizer._regenerate_invalid_heading",
        return_value="冒頭の挨拶と進行確認",
    )
    def test_fact_unsafe_heading_is_regenerated(
        self, regenerate
    ) -> None:
        first = "会議を始めます。よろしくお願いします。" * 8
        second = "次に研修内容を確認します。"
        text = first + second

        result = _assemble_sections_with_offsets(
            text,
            [
                (0, "99名での会議開始"),
                (len(first), "研修内容の確認"),
            ],
        )

        self.assertIn("### ▼冒頭の挨拶と進行確認", result)
        self.assertNotIn("### ▼（パート1）", result)
        regenerate.assert_called_once()

    def test_placeholder_heading_is_never_published(self) -> None:
        from transcript_section_summarizer import _finalize_heading

        with patch(
            "transcript_section_summarizer._regenerate_invalid_heading",
            return_value="",
        ):
            result = _finalize_heading(
                "99名での会議開始",
                "会議を始めます。よろしくお願いします。",
                1,
                None,
            )
        self.assertEqual(result, "この区間の議論")
        self.assertNotIn("パート", result)

    def test_generation_fails_closed_without_headings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "minutes_transcript_sections.add_section_headings",
                return_value="発言録だけ",
            ):
                with self.assertRaisesRegex(RuntimeError, "too few headings"):
                    generate_sectioned_transcript(
                        job_dir=tmp,
                        transcript_text="発言録だけ",
                    )

    def test_replaces_transcript_and_preserves_structured_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "minutes_draft.md").write_text(
                "# 会議\n\n## 発言録（整文）\n\n旧本文\n",
                encoding="utf-8",
            )
            (job / "minutes_structured.md").write_text(
                "# 会議\n\n## 決定事項\n\n- 決定\n\n"
                "## 発言録（整文）\n\n旧本文\n\n"
                "## 管理情報\n\n- internal\n",
                encoding="utf-8",
            )
            annotated = (
                "### ▼最初の論点\n\n本文1\n\n"
                "### ▼次の論点\n\n本文2\n"
            )

            result = write_sectioned_transcript_artifacts(
                job_dir=job,
                annotated_transcript=annotated,
            )

            draft = (job / "minutes_draft.md").read_text(encoding="utf-8")
            structured = (job / "minutes_structured.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(result["heading_count"], 2)
            self.assertIn("### ▼最初の論点", draft)
            self.assertNotIn("旧本文", draft)
            self.assertIn("## 決定事項", structured)
            self.assertIn("## 管理情報", structured)
            self.assertIn("### ▼次の論点", structured)
            self.assertNotIn("旧本文", structured)
            self.assertEqual(
                len(list(job.glob("*_pre_section_restore_*.md"))),
                2,
            )


class AnnotateTranscriptHeadingsTests(unittest.TestCase):
    def test_short_transcript_skips_headings(self) -> None:
        short = "短い本文です。"
        self.assertLess(len(short), INTEGRATED_MIN_INPUT_CHARS)
        self.assertEqual(
            _annotate_transcript_with_section_headings(short, "unused"),
            short,
        )

    def test_short_meeting_still_requires_headings(self) -> None:
        meeting = "日程を確認します。" * 50
        self.assertGreaterEqual(len(meeting), INTEGRATED_MIN_INPUT_CHARS)
        self.assertLess(len(meeting), 1500)
        with patch(
            "generate_minutes_transcript.generate_sectioned_transcript",
            return_value="### ▼日程確認\n\n" + meeting,
        ) as generate:
            result = _annotate_transcript_with_section_headings(
                meeting, "job_dir"
            )
        generate.assert_called_once()
        self.assertIn("### ▼日程確認", result)

    def test_long_transcript_requires_headings(self) -> None:
        long_text = "会議を始めます。" * 200
        self.assertGreaterEqual(len(long_text), INTEGRATED_MIN_INPUT_CHARS)
        with patch(
            "generate_minutes_transcript.generate_sectioned_transcript",
            return_value="### ▼導入\n\n" + long_text,
        ) as generate:
            result = _annotate_transcript_with_section_headings(
                long_text, "job_dir"
            )
        generate.assert_called_once_with(
            job_dir="job_dir",
            transcript_text=long_text,
        )
        self.assertIn("### ▼導入", result)

    def test_long_transcript_without_headings_fails_closed(self) -> None:
        long_text = "会議を始めます。" * 200
        with patch(
            "generate_minutes_transcript.generate_sectioned_transcript",
            side_effect=RuntimeError("too few headings: 0 < 2"),
        ):
            with self.assertRaisesRegex(RuntimeError, "too few headings"):
                _annotate_transcript_with_section_headings(
                    long_text, "job_dir"
                )


if __name__ == "__main__":
    unittest.main()
