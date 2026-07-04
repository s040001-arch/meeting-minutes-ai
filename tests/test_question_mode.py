"""Tests for QUESTION_MODE pause / resume control (no API calls)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from question_mode import (
    build_questions_review_md,
    clear_pause_marker,
    clear_pause_on_terminal,
    count_pending_unknowns,
    has_pending_unknowns,
    is_paused,
    resolve_question_mode,
    should_pause_for_answers,
    should_send_line,
    write_pause_marker,
    write_questions_review_md,
)


class ResolveModeTests(unittest.TestCase):
    def test_default_off(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("QUESTION_MODE", None)
            self.assertEqual(resolve_question_mode(), "off")
            self.assertFalse(should_pause_for_answers())
            self.assertFalse(should_send_line())

    def test_off_aliases(self) -> None:
        for v in ("off", "OFF", "false", "0", ""):
            self.assertEqual(resolve_question_mode(v), "off", v)

    def test_cursor_aliases(self) -> None:
        for v in ("on", "cursor", "md", "true", "1"):
            self.assertEqual(resolve_question_mode(v), "cursor", v)
            self.assertTrue(should_pause_for_answers(v))
            self.assertFalse(should_send_line(v))

    def test_line_mode(self) -> None:
        self.assertEqual(resolve_question_mode("line"), "line")
        self.assertTrue(should_pause_for_answers("line"))
        self.assertTrue(should_send_line("line"))

    def test_unknown_fails_closed_to_off(self) -> None:
        self.assertEqual(resolve_question_mode("weird"), "off")


class PauseMarkerTests(unittest.TestCase):
    def test_write_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_pause_marker(
                tmp,
                mode="cursor",
                question_artifacts=["questions_review.md"],
                resume_hint="resume me",
            )
            self.assertTrue(path.is_file())
            self.assertTrue(is_paused(tmp))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "paused_waiting_answers")
            self.assertEqual(data["mode"], "cursor")
            self.assertIn("resume", data["resume_command"])
            self.assertTrue(clear_pause_marker(tmp))
            self.assertFalse(is_paused(tmp))
            self.assertFalse(clear_pause_marker(tmp))

    def test_terminal_status_clears_stale_pause_and_is_paused_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            write_pause_marker(
                job,
                mode="line",
                question_artifacts=[],
                resume_hint="x",
            )
            self.assertTrue(is_paused(job))
            (job / "progress.json").write_text(
                json.dumps({"overall_status": "success", "phase": "done"}),
                encoding="utf-8",
            )
            # success wins over stale marker
            self.assertFalse(is_paused(job))
            self.assertTrue(clear_pause_on_terminal(job, "success"))
            self.assertFalse((job / "question_pause.json").is_file())
            self.assertFalse(clear_pause_on_terminal(job, "paused"))

    def test_count_pending_unknowns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "unknown_points.json").write_text(
                json.dumps(
                    [
                        {"type": "coherence_review", "anomaly_word": "a", "status": "open"},
                        {"type": "coherence_review", "anomaly_word": "b", "status": "asked"},
                        {"type": "coherence_review", "anomaly_word": "c", "status": "answered"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(count_pending_unknowns(job), 2)
            self.assertTrue(has_pending_unknowns(job))


class QuestionsReviewMdTests(unittest.TestCase):
    def test_assembles_reader_and_line_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "reader_pass_questions.md").write_text(
                "# reader\n\n## #1\n\n質問タイトル\n\n"
                "**該当テキスト**\n\n> 基本形式\n\n"
                "**なぜ分からないか**\n\n曖昧\n\n"
                "**質問**\n\n任意ですか?\n\n→ 回答:\n\n---\n",
                encoding="utf-8",
            )
            md = build_questions_review_md(job, job_id="job_x")
            self.assertIn("job_x", md)
            self.assertIn("Reader pass", md)
            self.assertIn("任意ですか?", md)
            self.assertIn("--from-step resume", md)
            out = write_questions_review_md(job, job_id="job_x")
            self.assertEqual(out.name, "questions_review.md")
            self.assertTrue(out.is_file())
            self.assertTrue((job / "integrated_questions.json").is_file())


class ReprocessResumeTests(unittest.TestCase):
    def test_resume_maps_to_6_1_and_clears_pause(self) -> None:
        from reprocess_job import reprocess

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job_resume"
            job.mkdir()
            (job / "merged_transcript_after_qa.txt").write_text("body", encoding="utf-8")
            (job / "google_doc_hub.json").write_text(
                json.dumps({"doc_id": "doc123"}), encoding="utf-8"
            )
            write_pause_marker(
                job,
                mode="on",
                question_artifacts=[],
                resume_hint="x",
            )
            self.assertTrue(is_paused(job))

            with patch("reprocess_job.run_step", return_value={"status": "ok"}) as mock_run:
                with patch("reprocess_job.verify_doc_pattern", return_value={
                    "found": True, "count": 0, "doc_chars": 10, "pattern": "",
                }):
                    result = reprocess(
                        job,
                        input_root=str(job.parent),
                        from_step="resume",
                        input_override=None,
                        verify_pattern="",
                        credentials="credentials.json",
                        token="token.json",
                        docs_chunk_size=5000,
                        reason="",
                        dry_run=False,
                    )

            self.assertTrue(result["resume_from_pause"])
            self.assertEqual(result["from_step"], "resume")
            self.assertEqual(result["steps_run"], ["6.1", "6.2", "6.3"])
            self.assertFalse(is_paused(job))
            self.assertEqual(mock_run.call_count, 3)


if __name__ == "__main__":
    unittest.main()
