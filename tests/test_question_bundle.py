"""Tests for Phase 10 step-2 safe question bundling."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from question_bundle import (
    BUNDLE_KIND_REPLACE,
    anomalies_suggest_single_landing,
    bundle_safe_answer_items,
    effective_landing,
    expand_answer_items_for_apply,
    normalize_hypothesis,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "fixtures" / "phase10_2_4_164142_answers_template.json"


class EffectiveLandingTests(unittest.TestCase):
    def test_keep_maps_to_hypothesis(self) -> None:
        self.assertEqual(effective_landing("正しい", "THR"), "THR")

    def test_override_differs(self) -> None:
        self.assertNotEqual(
            effective_landing("THRさん", "季央"),
            effective_landing("正しい", "季央"),
        )


class AnomalyHeuristicTests(unittest.TestCase):
    def test_kio_split(self) -> None:
        self.assertFalse(
            anomalies_suggest_single_landing("季央", ["TOKIO", "定着さん"])
        )

    def test_thr_merge(self) -> None:
        self.assertTrue(anomalies_suggest_single_landing("THR", ["dhr", "Dhr"]))


class Bundle164142Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE.is_file():
            raise unittest.SkipTest(f"missing fixture {FIXTURE}")
        doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.raw_answers = list(doc.get("answers") or [])

    def test_safe_bundle_count(self) -> None:
        bundled = bundle_safe_answer_items(self.raw_answers)
        self.assertEqual(len(bundled), 42)
        with_targets = [b for b in bundled if b.get("targets")]
        self.assertEqual(len(with_targets), 5)

    def test_kio_never_merged(self) -> None:
        bundled = bundle_safe_answer_items(self.raw_answers)
        kio_ids = {
            a["proposal_id"]
            for a in self.raw_answers
            if normalize_hypothesis(a.get("hypothesis")) == normalize_hypothesis("季央")
        }
        for entry in bundled:
            targets = entry.get("targets") or []
            if len(targets) < 2:
                continue
            tids = {t.get("proposal_id") for t in targets}
            overlap = kio_ids & tids
            self.assertLessEqual(len(overlap), 1, msg=f"季央 bundled: {overlap}")

    def test_thr_bundle_has_two_targets(self) -> None:
        bundled = bundle_safe_answer_items(self.raw_answers)
        thr = [
            b
            for b in bundled
            if normalize_hypothesis(b.get("hypothesis")) == "THR" and b.get("targets")
        ]
        self.assertEqual(len(thr), 1)
        self.assertEqual(len(thr[0]["targets"]), 2)


class ApplyExpandTests(unittest.TestCase):
    def test_expand_thr_bundle(self) -> None:
        bundle = {
            "question_id": "q-thr",
            "answer_text": "正しい",
            "bundle_kind": BUNDLE_KIND_REPLACE,
            "hypothesis": "THR",
            "targets": [
                {
                    "proposal_id": "p1",
                    "anomaly_word": "dhr",
                    "span_before": "dhr チーム",
                    "span_start": 10,
                    "hypothesis": "THR",
                },
                {
                    "proposal_id": "p2",
                    "anomaly_word": "Dhr",
                    "span_before": "Dhr 向け",
                    "span_start": 50,
                    "hypothesis": "THR",
                },
            ],
        }
        expanded = expand_answer_items_for_apply([bundle])
        self.assertEqual(len(expanded), 2)
        self.assertTrue(all(e["answer_text"] == "正しい" for e in expanded))
        self.assertEqual({e["question_id"] for e in expanded}, {"q-thr"})

    def test_single_without_targets_unchanged(self) -> None:
        single = {
            "question_id": "q1",
            "answer_text": "正しい",
            "anomaly_word": "foo",
            "span_before": "foo bar",
            "span_start": 0,
            "hypothesis": "bar",
        }
        expanded = expand_answer_items_for_apply([single])
        self.assertEqual(len(expanded), 1)
        self.assertIs(expanded[0], single)


class ApplyThrIntegrationTests(unittest.TestCase):
    def test_one_answer_fixes_two_spans(self) -> None:
        from scripts.apply_phase10_step2_answers import apply_answers

        text = "会議は dhr チームと Dhr 向けの話です。"
        bundle = {
            "question_id": "q-thr",
            "answer_text": "正しい",
            "bundle_kind": BUNDLE_KIND_REPLACE,
            "hypothesis": "THR",
            "targets": [
                {
                    "anomaly_word": "dhr",
                    "span_before": "dhr チーム",
                    "span_start": 3,
                    "hypothesis": "THR",
                },
                {
                    "anomaly_word": "Dhr",
                    "span_before": "Dhr 向け",
                    "span_start": 11,
                    "hypothesis": "THR",
                },
            ],
        }
        out, applied = apply_answers(text, [bundle])
        self.assertIn("THR チーム", out)
        self.assertIn("THR 向け", out)
        self.assertEqual(len([a for a in applied if not a.get("error")]), 2)


if __name__ == "__main__":
    unittest.main()
