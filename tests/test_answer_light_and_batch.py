"""Tests for QUESTION_MODE light resume + coherence recognition_batch (no API)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recognition_batch import (
    RECOGNITION_BATCH_FORMAT,
    build_batch_items,
    build_batch_question_text,
)
from run_question_cycle_once import (
    _build_coherence_single_question_payload,
    _mark_unknown_point_asked,
)
from recorrect_from_line_answer import _mark_batch_items_answered_in_unknowns


class CoherenceBatchWhenPausedTests(unittest.TestCase):
    def test_batch_question_shows_context_highlight_and_candidate(self) -> None:
        points = [
            {
                "type": "coherence_review",
                "anomaly_id": "ta_001",
                "anomaly_word": "リング不足",
                "text": "リング不足",
                "span_text": "人材のリング不足みたいなところをどう埋めるか",
                "estimated_correction": "リソース不足",
                "confidence": "medium",
                "context_position_in_transcript": 10,
            },
            {
                "type": "coherence_review",
                "anomaly_id": "ta_002",
                "anomaly_word": "倫理決済",
                "text": "倫理決済",
                "span_text": "役員さんの倫理決済みたいなプロセスが必要",
                "estimated_correction": "稟議決裁",
                "confidence": "medium",
                "context_position_in_transcript": 20,
            },
        ]
        items = build_batch_items(points)
        text = build_batch_question_text(items)
        self.assertIn("【リング不足】", text)
        self.assertIn("→「リソース不足」？", text)
        self.assertIn("【倫理決済】", text)
        self.assertIn("→「稟議決裁」？", text)
        self.assertIn("人材の", text)
        self.assertNotIn("ドライマンゴー", text)

    def test_batch_payload_when_question_mode_line(self) -> None:
        pending = [
            {
                "type": "coherence_review",
                "source": "coherence_review",
                "anomaly_id": "ta_001",
                "anomaly_word": "リング不足",
                "text": "リング不足",
                "confidence": "medium",
                "estimated_correction": "リンク不足",
                "context_position_in_transcript": 10,
            },
            {
                "type": "coherence_review",
                "source": "coherence_review",
                "anomaly_id": "ta_002",
                "anomaly_word": "倫理決済",
                "text": "倫理決済",
                "confidence": "medium",
                "estimated_correction": "稟議決裁",
                "context_position_in_transcript": 20,
            },
            {
                "type": "coherence_review",
                "source": "coherence_review",
                "anomaly_id": "ta_003",
                "anomaly_word": "会議祭",
                "text": "会議祭",
                "confidence": "low",
                "estimated_correction": "",
                "context_position_in_transcript": 30,
            },
        ]
        with patch("question_mode.should_pause_for_answers", return_value=True):
            with patch(
                "run_question_cycle_once.write_line_pending_context"
            ) as mock_ctx:
                payload = _build_coherence_single_question_payload(
                    job_id="job_x",
                    coherence_pending=pending,
                    pending_meta={"pending_unknown_points_count": 3},
                    doc_url="",
                )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["question_format"], RECOGNITION_BATCH_FORMAT)
        self.assertEqual(payload["question_status"], "generated")
        items = payload["selected_unknown"]["batch_items"]
        self.assertEqual(len(items), 3)
        self.assertIn("リング不足", payload["question_text"])
        self.assertIn("倫理決済", payload["question_text"])
        mock_ctx.assert_called_once()

    def test_fifo_single_when_question_mode_off(self) -> None:
        pending = [
            {
                "type": "coherence_review",
                "source": "coherence_review",
                "anomaly_id": "ta_001",
                "anomaly_word": "リング不足",
                "text": "リング不足",
                "confidence": "medium",
                "estimated_correction": "リンク不足",
                "context_position_in_transcript": 10,
            },
            {
                "type": "coherence_review",
                "source": "coherence_review",
                "anomaly_id": "ta_002",
                "anomaly_word": "倫理決済",
                "text": "倫理決済",
                "confidence": "medium",
                "estimated_correction": "稟議決裁",
                "context_position_in_transcript": 20,
            },
        ]
        with patch("question_mode.should_pause_for_answers", return_value=False):
            with patch("run_question_cycle_once.write_line_pending_context"):
                payload = _build_coherence_single_question_payload(
                    job_id="job_x",
                    coherence_pending=pending,
                    pending_meta={},
                    doc_url="",
                )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["question_format"], "free_text")
        self.assertEqual(
            payload["selected_unknown"]["anomaly_id"], "ta_001"
        )
        self.assertNotIn("batch_items", payload["selected_unknown"])


class MarkBatchAskedAndAnsweredTests(unittest.TestCase):
    def test_mark_asked_covers_all_batch_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unknowns = Path(tmp) / "unknown_points.json"
            points = [
                {
                    "type": "coherence_review",
                    "anomaly_id": "ta_001",
                    "anomaly_word": "リング不足",
                    "text": "リング不足",
                    "status": "open",
                },
                {
                    "type": "coherence_review",
                    "anomaly_id": "ta_002",
                    "anomaly_word": "倫理決済",
                    "text": "倫理決済",
                    "status": "open",
                },
            ]
            unknowns.write_text(
                json.dumps(points, ensure_ascii=False), encoding="utf-8"
            )
            selected = {
                "type": "recognition_batch",
                "batch_items": build_batch_items(points),
                "anomaly_id": "ta_001",
                "text": "リング不足",
            }
            n = _mark_unknown_point_asked(
                str(unknowns), selected, question_id="q1"
            )
            self.assertEqual(n, 2)
            data = json.loads(unknowns.read_text(encoding="utf-8"))
            self.assertTrue(all(x["status"] == "asked" for x in data))

    def test_unresolved_batch_items_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            job_id = "job_x"
            root = str(job)
            # layout: input_root/job_id/unknown_points.json
            jdir = job / job_id
            jdir.mkdir()
            points = [
                {
                    "type": "coherence_review",
                    "anomaly_id": "ta_001",
                    "anomaly_word": "リング不足",
                    "status": "asked",
                    "asked_by_question_id": "q1",
                },
                {
                    "type": "coherence_review",
                    "anomaly_id": "ta_002",
                    "anomaly_word": "倫理決済",
                    "status": "asked",
                    "asked_by_question_id": "q1",
                },
            ]
            (jdir / "unknown_points.json").write_text(
                json.dumps(points, ensure_ascii=False), encoding="utf-8"
            )
            batch_items = [
                {"anomaly_id": "ta_001", "word": "リング不足"},
                {"anomaly_id": "ta_002", "word": "倫理決済"},
            ]
            parsed = [
                {"anomaly_id": "ta_001", "word": "リング不足", "action": "correct"},
                {"anomaly_id": "ta_002", "word": "倫理決済", "action": "unknown"},
            ]
            n = _mark_batch_items_answered_in_unknowns(
                job_id=job_id,
                input_root=root,
                parsed=parsed,
                answer_text="1 リンク不足 / 2 不明",
                question_id="q1",
                batch_items=batch_items,
            )
            self.assertEqual(n, 2)
            data = json.loads(
                (jdir / "unknown_points.json").read_text(encoding="utf-8")
            )
            by_id = {x["anomaly_id"]: x for x in data}
            self.assertEqual(by_id["ta_001"]["status"], "answered")
            self.assertEqual(by_id["ta_002"]["status"], "open")


class WebhookLightResumeBranchTests(unittest.TestCase):
    def test_light_path_when_question_mode_line(self) -> None:
        from webhook_app import maybe_launch_auto_after_answer

        with patch.dict(os.environ, {"QUESTION_MODE": "line", "AUTO_AFTER_ANSWER": "1"}):
            with patch("webhook_app._try_acquire_auto_after_answer_lock", return_value=None):
                # lock fails → no launch, but we can inspect the branch via launch_label path
                with patch("webhook_app._launch_resume_subprocess") as mock_launch:
                    maybe_launch_auto_after_answer("job_x", save_ok=True)
                    mock_launch.assert_called_once()
                    kwargs = mock_launch.call_args.kwargs
                    self.assertEqual(kwargs["launch_label"], "auto_after_answer_light")
                    self.assertIn("run_answer_light.py", kwargs["cmd"][1])

    def test_legacy_path_when_question_mode_off(self) -> None:
        from webhook_app import maybe_launch_auto_after_answer

        with patch.dict(os.environ, {"QUESTION_MODE": "off", "AUTO_AFTER_ANSWER": "1"}, clear=False):
            os.environ.pop("QUESTION_MODE", None)
            with patch.dict(os.environ, {"QUESTION_MODE": "off"}):
                with patch("webhook_app._launch_resume_subprocess") as mock_launch:
                    maybe_launch_auto_after_answer("job_x", save_ok=True)
                    mock_launch.assert_called_once()
                    kwargs = mock_launch.call_args.kwargs
                    self.assertEqual(kwargs["launch_label"], "auto_after_answer")
                    self.assertIn("run_docs_hub_e2e.py", kwargs["cmd"][1])


class PrefillRemovedTests(unittest.TestCase):
    def test_knowledge_sheet_store_no_assistant_prefill(self) -> None:
        import inspect
        import knowledge_sheet_store as kss

        src = inspect.getsource(kss._merge_knowledge_memos_with_claude)
        self.assertNotIn('"role": "assistant"', src)
        src2 = inspect.getsource(kss._merge_knowledge_memos_with_all_answers)
        self.assertNotIn('"role": "assistant"', src2)


if __name__ == "__main__":
    unittest.main()
