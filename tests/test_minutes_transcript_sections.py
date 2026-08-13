from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minutes_transcript_sections import (
    generate_sectioned_transcript,
    write_sectioned_transcript_artifacts,
)


class MinutesTranscriptSectionsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
