"""Regression: span incorporate must not roll back incremental after_qa reflects."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from recorrect_from_line_answer import _load_recorrect_base_text


class RecorrectBaseTextTests(unittest.TestCase):
    def test_prefers_after_qa_over_ai(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = os.path.join(tmp, "job_x")
            os.makedirs(job_dir)
            ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
            after_path = os.path.join(job_dir, "merged_transcript_after_qa.txt")
            with open(ai_path, "w", encoding="utf-8") as f:
                f.write("早く若い人[要確認]はあれですか?")
            with open(after_path, "w", encoding="utf-8") as f:
                f.write("はあれですか?")

            text, path = _load_recorrect_base_text(job_dir, None)
            self.assertEqual(text, "はあれですか?")
            self.assertTrue(path.endswith("merged_transcript_after_qa.txt"))


if __name__ == "__main__":
    unittest.main()
