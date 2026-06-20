"""Tests: recorrect ②③ uses pinpoint apply (no LLM incorporate)."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fact_integrity_gate import verify_fact_integrity
from pinpoint_answer_apply import apply_answers
from question_bundle import bundle_safe_answer_items, expand_answer_items_for_apply
from recorrect_from_line_answer import (
    _build_editor_apply_record,
    _handle_editor_pinpoint_answer,
    _is_contextual_editor_question,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "fixtures" / "phase10_2_4_164142_answers_template.json"
MECH = ROOT / "data" / "164142_railway_mechanical.txt"


class EditorDetectTests(unittest.TestCase):
    def test_contextual_editor_from_selected_unknown(self) -> None:
        qr = {
            "selected_unknown": {
                "type": "contextual_editor",
                "source": "contextual_editor",
                "verdict": "ask_with_candidate",
                "span_text": "私と TOKIO さんが参加していて",
                "anomaly_word": "TOKIO",
            }
        }
        self.assertTrue(_is_contextual_editor_question(qr))

    def test_coherence_not_editor(self) -> None:
        qr = {
            "selected_unknown": {
                "type": "coherence_review",
                "source": "coherence_review",
                "anomaly_word": "7.5万円",
            }
        }
        self.assertFalse(_is_contextual_editor_question(qr))


class PinpointSingleAnswerTests(unittest.TestCase):
    def test_tokio_to_kio_no_surrounding_damage(self) -> None:
        text = (
            "お疲れ様。お願いします。今日の進め方ですけれども、"
            "あの仕事力サービスチームとして、私と TOKIO さんが参加していてっていう形なんですが。"
        )
        item = {
            "answer_text": "正しい",
            "anomaly_word": "TOKIO",
            "span_before": "私と TOKIO さんが参加していて",
            "span_start": text.find("私と"),
            "hypothesis": "季央",
            "selected_unknown": {"type": "contextual_editor", "source": "contextual_editor"},
        }
        out, applied = apply_answers(text, [item])
        self.assertEqual(out.count("。。"), 0)
        self.assertEqual(out.count("お願いします"), 1)
        self.assertIn("季央", out)
        self.assertNotIn("TOKIO", out)
        self.assertEqual(len([a for a in applied if not a.get("error")]), 1)

    def test_handle_editor_pinpoint_writes_after_qa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_id = "job_pinpoint"
            job_dir = os.path.join(tmp, job_id)
            os.makedirs(job_dir)
            base = "私と TOKIO さんが参加していて。"
            ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
            with open(ai_path, "w", encoding="utf-8") as f:
                f.write(base)
            qr = {
                "selected_unknown": {
                    "type": "contextual_editor",
                    "source": "contextual_editor",
                    "verdict": "ask_with_candidate",
                    "span_text": "私と TOKIO さんが参加していて",
                    "anomaly_word": "TOKIO",
                    "hypothesis": "季央",
                    "span_start": 0,
                }
            }
            record = {"question_id": "q1", "answer_text": "正しい"}
            out_path = os.path.join(job_dir, "merged_transcript_after_qa.txt")
            with patch(
                "recorrect_from_line_answer._persist_coherence_answer_to_learned_dict",
                return_value={"action": "noop"},
            ):
                _handle_editor_pinpoint_answer(
                    job_id=job_id,
                    input_root=tmp,
                    question_result=qr,
                    record=record,
                    out_path=out_path,
                )
            with open(out_path, encoding="utf-8") as f:
                after = f.read()
            self.assertIn("季央", after)
            self.assertNotIn("TOKIO", after)


class BundleTargetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE.is_file():
            raise unittest.SkipTest(f"missing {FIXTURE}")
        doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.raw = list(doc.get("answers") or [])

    def test_thr_bundle_expands_to_two_spans(self) -> None:
        bundled = bundle_safe_answer_items(self.raw)
        thr = [
            b
            for b in bundled
            if b.get("targets") and b.get("hypothesis") == "THR"
        ]
        self.assertEqual(len(thr), 1)
        expanded = expand_answer_items_for_apply(
            [{**thr[0], "answer_text": "正しい"}]
        )
        self.assertEqual(len(expanded), 2)

    def test_build_record_from_targets(self) -> None:
        bundled = bundle_safe_answer_items(self.raw)
        thr = next(b for b in bundled if b.get("targets") and b.get("hypothesis") == "THR")
        rec = _build_editor_apply_record(
            thr,
            {"question_id": "q-thr", "answer_text": "正しい"},
        )
        self.assertEqual(len(rec.get("targets") or []), 2)


class Recorrect164142IncrementalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE.is_file() or not MECH.is_file():
            raise unittest.SkipTest("missing 164142 fixture or mechanical snapshot")
        cls.answers = json.loads(FIXTURE.read_text(encoding="utf-8"))["answers"]
        cls.mechanical = MECH.read_text(encoding="utf-8")

    def test_47_answers_incremental_no_double_period(self) -> None:
        text = self.mechanical
        errors = 0
        for ans in self.answers:
            qr = {k: ans[k] for k in ans if k != "answer_text"}
            rec = _build_editor_apply_record(qr, ans)
            text, applied = apply_answers(text, [rec])
            errors += sum(1 for a in applied if a.get("error"))
        self.assertEqual(errors, 0, "some pinpoint applies failed")
        self.assertEqual(len(re.findall(r"[。．]{2,}", text)), 0)
        opening = text[:120]
        self.assertNotIn("。。", opening)
        self.assertNotRegex(opening, r"お疲れ様[。．]+お疲れ様")
        self.assertNotRegex(opening, r"お願いします[。．]+お願いします")
        self.assertIn("季央", text)
        self.assertIn("THR", text)
        self.assertIn("仕事力サーベイ", text)
        gate = verify_fact_integrity(self.mechanical, text)
        regressions = [
            v
            for v in gate.violations
            if "_missing" in v or "decreased" in v or "flagged_token_missing" in v
        ]
        self.assertEqual(regressions, [], msg=gate.violations)


if __name__ == "__main__":
    unittest.main()
