"""Tests for integrated_questions (cascade groups + MD, no API)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from integrated_questions import (
    build_cascade_questions,
    build_integrated_md,
    select_next_line_bundle,
    write_integrated_questions,
)


def _proposal(
    *,
    pid: str,
    word: str,
    hyp: str,
    span: str,
    start: int,
    fact_class: str = "proper_noun",
    verdict: str = "ask_with_candidate",
) -> dict:
    return {
        "proposal_id": pid,
        "verdict": verdict,
        "anomaly_word": word,
        "hypothesis": hyp,
        "span_before": span,
        "span_start": start,
        "context_position_in_transcript": start,
        "fact_class": fact_class,
        "reason": "test",
        "applied": False,
    }


class CascadeGroupTests(unittest.TestCase):
    def test_same_hypothesis_bundles_with_cascade_note(self) -> None:
        proposals = [
            _proposal(pid="1", word="大瀬さん", hyp="合瀬さん", span="左が大瀬さん", start=10),
            _proposal(pid="2", word="厚さん", hyp="合瀬さん", span="厚さんの部署", start=100),
            _proposal(pid="3", word="独自語", hyp="別物", span="独自語です", start=200),
        ]
        qs = build_cascade_questions(proposals)
        bundles = [q for q in qs if q.get("is_bundle")]
        self.assertEqual(len(bundles), 1)
        self.assertEqual(len(bundles[0]["targets"]), 2)
        self.assertIn("確定します", bundles[0]["cascade_note"])
        # 3 proposals → 2 questions (1 bundle + 1 single)
        self.assertEqual(len(qs), 2)

    def test_kio_style_not_merged(self) -> None:
        proposals = [
            _proposal(pid="a", word="TOKIO", hyp="季央", span="私と TOKIO", start=1),
            _proposal(pid="b", word="定着さん", hyp="季央", span="定着さんも", start=50),
        ]
        qs = build_cascade_questions(proposals)
        bundles = [q for q in qs if q.get("is_bundle")]
        self.assertEqual(len(bundles), 0)
        self.assertEqual(len(qs), 2)


class IntegratedMdTests(unittest.TestCase):
    def test_write_md_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job_x"
            job.mkdir()
            proposals = [
                _proposal(pid="1", word="大瀬さん", hyp="合瀬さん", span="左が大瀬さん", start=10),
                _proposal(pid="2", word="厚さん", hyp="合瀬さん", span="厚さん部署", start=80),
            ]
            (job / "edit_proposals.json").write_text(
                json.dumps({"proposals": proposals}, ensure_ascii=False),
                encoding="utf-8",
            )
            (job / "reader_pass_result.json").write_text(
                json.dumps({
                    "findings": [{
                        "rank": 1,
                        "excerpt": "基本形式",
                        "reason": "曖昧",
                        "question": "任意ですか?",
                    }]
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            (job / "merged_transcript_ai.txt").write_text(
                "あれ左が大瀬さんですか? " + ("あ" * 50) + "厚さん部署",
                encoding="utf-8",
            )
            path, stats = write_integrated_questions(job, job_id="job_x")
            self.assertTrue(path.is_file())
            md = path.read_text(encoding="utf-8")
            self.assertIn("波及", md)
            self.assertIn("合瀬さん", md)
            self.assertIn("Section A", md)
            self.assertIn("任意ですか?", md)
            self.assertEqual(stats["cascade_groups"], 1)
            self.assertEqual(stats["ask_raw_spans"], 2)
            self.assertEqual(stats["ask_questions"], 1)
            self.assertTrue((job / "integrated_questions.json").is_file())


class LineBundleSelectTests(unittest.TestCase):
    def test_prefers_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job_line"
            job.mkdir()
            proposals = [
                _proposal(pid="1", word="大瀬さん", hyp="合瀬さん", span="左が大瀬さん", start=10),
                _proposal(pid="2", word="厚さん", hyp="合瀬さん", span="厚さん部署", start=80),
                _proposal(pid="3", word="独自", hyp="独自正解", span="独自です", start=200),
            ]
            (job / "edit_proposals.json").write_text(
                json.dumps({"proposals": proposals}, ensure_ascii=False),
                encoding="utf-8",
            )
            payload = select_next_line_bundle(job)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertTrue(payload["is_bundle"])
            self.assertEqual(payload["question_format"], "bundle")
            self.assertEqual(len(payload["targets"]), 2)
            self.assertIn("targets", payload["selected_unknown"])


class DeferLineBundleTests(unittest.TestCase):
    def test_should_defer_after_recognition_batch(self) -> None:
        from integrated_questions import should_defer_line_bundle

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job_defer"
            job.mkdir()
            (job / "question_result.json").write_text(
                json.dumps(
                    {
                        "question_status": "generated",
                        "question_format": "recognition_batch",
                        "selected_unknown": {"batch_items": [{"word": "決済"}]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertTrue(should_defer_line_bundle(job))

    def test_save_deferred_bundle(self) -> None:
        from integrated_questions import (
            DEFERRED_LINE_BUNDLE_FILENAME,
            save_deferred_line_bundle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job_save"
            job.mkdir()
            proposals = [
                _proposal(pid="1", word="野村", hyp="野村不動産", span="野村です", start=10),
            ]
            (job / "edit_proposals.json").write_text(
                json.dumps({"proposals": proposals}, ensure_ascii=False),
                encoding="utf-8",
            )
            meta = save_deferred_line_bundle(job)
            self.assertTrue(meta.get("saved"))
            self.assertTrue((job / DEFERRED_LINE_BUNDLE_FILENAME).is_file())


class BuildMdStatsTests(unittest.TestCase):
    def test_empty_inputs(self) -> None:
        md, stats = build_integrated_md(
            job_id="j",
            reader_findings=[],
            cascade_questions=[],
            transcript="",
        )
        self.assertIn("所見なし", md)
        self.assertEqual(stats["ask_questions"], 0)


if __name__ == "__main__":
    unittest.main()
