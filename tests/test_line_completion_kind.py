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


if __name__ == "__main__":
    unittest.main()
