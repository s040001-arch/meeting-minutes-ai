from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cursor_question_bridge import (
    append_job_answer,
    launch_answer_resume,
    pending_jobs,
    public_question,
)
from question_mode import write_pause_marker


class CursorQuestionBridgeTests(unittest.TestCase):
    def _paused_job(self, root: Path, name: str = "job_20260808_001") -> Path:
        job = root / name
        job.mkdir()
        (job / "question_result.json").write_text(
            json.dumps(
                {
                    "question_status": "generated",
                    "question_id": "q-1",
                    "question_format": "free_text",
                    "question_text": "正しい表現を教えてください。",
                    "doc_url": "https://example.test/doc",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        write_pause_marker(
            job,
            mode="cursor",
            question_artifacts=["question_result.json"],
            resume_hint="answer",
        )
        return job

    def test_lists_only_paused_generated_questions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self._paused_job(root)
            done = root / "job_20260808_002"
            done.mkdir()
            (done / "question_result.json").write_text(
                json.dumps({"question_status": "none"}),
                encoding="utf-8",
            )

            found = pending_jobs(root)

            self.assertEqual([path for path, _ in found], [job])
            self.assertEqual(
                public_question(*found[0])["question_text"],
                "正しい表現を教えてください。",
            )

    def test_appends_job_scoped_answer_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self._paused_job(Path(tmp))
            payload = json.loads(
                (job / "question_result.json").read_text(encoding="utf-8")
            )

            path, first = append_job_answer(job, payload, "正しくは山屋です")
            _, second = append_job_answer(job, payload, "別の回答")

            self.assertTrue(first)
            self.assertFalse(second)
            rows = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["channel"], "cursor")
            self.assertEqual(rows[0]["job_id"], job.name)

    @patch("cursor_question_bridge.subprocess.Popen")
    def test_resume_forces_cursor_mode_without_line_flag(
        self, popen: Mock
    ) -> None:
        popen.return_value.pid = 4321
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self._paused_job(root)
            answers = job / "answers.json"
            answers.write_text("[]", encoding="utf-8")

            pid = launch_answer_resume(
                job_dir=job,
                input_root=root,
                answers_path=answers,
            )

            self.assertEqual(pid, 4321)
            args, kwargs = popen.call_args
            command = args[0]
            self.assertNotIn("--send-line", command)
            self.assertEqual(kwargs["env"]["QUESTION_MODE"], "cursor")


if __name__ == "__main__":
    unittest.main()
