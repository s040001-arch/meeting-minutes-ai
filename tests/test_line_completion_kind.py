"""Tests for LINE completion_kind labels."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from line_send_question import build_line_message
from run_answer_light import send_completion_line


class LineCompletionKindTests(unittest.TestCase):
    def test_full_completion_label(self) -> None:
        msg = build_line_message(
            {"question_status": "none", "completion_kind": "full", "message": "完了です。"}
        )
        self.assertTrue(msg.startswith("[完了]"))

    def test_coherence_done_label(self) -> None:
        msg = build_line_message(
            {
                "question_status": "none",
                "completion_kind": "coherence_done",
                "message": "音声認識ゆれの確認は完了しました。",
            }
        )
        self.assertTrue(msg.startswith("[認識ゆれの確認は完了]"))
        self.assertNotIn("[完了]", msg.splitlines()[0])

    def test_answer_light_completion_message_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "google_doc_hub.json").write_text(
                json.dumps({"doc_id": "doc123"}), encoding="utf-8"
            )
            with patch("line_send_question.push_line_message") as mock_push:
                with patch.dict(
                    "os.environ",
                    {
                        "LINE_USER_ID": "U1",
                        "LINE_CHANNEL_ACCESS_TOKEN": "tok",
                    },
                ):
                    result = send_completion_line(
                        job, job_id="job_x", send_line=True
                    )
            self.assertTrue(result["sent"])
            self.assertEqual(result["reason"], "sent_completion")
            msg = result["message"]
            self.assertTrue(msg.startswith("[完了]"))
            self.assertIn("完了しました", msg)
            self.assertIn("docs.google.com", msg)
            mock_push.assert_called_once()
            written = json.loads(
                (job / "question_result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(written["question_status"], "none")
            self.assertEqual(written["completion_kind"], "full")

    def test_question_cycle_only_pushes_line_for_new_questions(self) -> None:
        from run_question_cycle_once import should_push_line_for_result

        self.assertTrue(should_push_line_for_result("generated"))
        self.assertFalse(should_push_line_for_result("none"))
        self.assertFalse(should_push_line_for_result(""))

    def test_recognition_batch_uses_source_transcript_url(self) -> None:
        msg = build_line_message(
            {
                "question_status": "generated",
                "question_format": "recognition_batch",
                "question_text": "1. テスト",
                "doc_url": "https://docs.google.com/document/d/abc/edit",
                "source_transcript_url": "https://drive.google.com/file/d/xyz/view",
            }
        )
        self.assertIn("参照（アップした原文・検索用）", msg)
        self.assertIn("drive.google.com/file/d/xyz/view", msg)
        self.assertNotIn("該当語は載っていません", msg)
        self.assertNotIn("docs.google.com", msg)

    def test_recognition_batch_omits_link_without_source(self) -> None:
        msg = build_line_message(
            {
                "question_status": "generated",
                "question_format": "recognition_batch",
                "question_text": "1. テスト",
                "doc_url": "https://docs.google.com/document/d/abc/edit",
            }
        )
        self.assertNotIn("docs.google.com", msg)
        self.assertNotIn("drive.google.com", msg)


if __name__ == "__main__":
    unittest.main()
