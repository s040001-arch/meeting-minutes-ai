from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minutes_quality_gate import (
    MinutesQualityGateError,
    _queue_unresolved_final_findings,
    evaluate_minutes_quality,
    run_minutes_quality_gate,
)


class MinutesQualityGateTests(unittest.TestCase):
    def _stats(self, *, findings=None, failed=None) -> dict:
        return {
            "total_chunks": 3,
            "failed_chunk_idx": list(failed or []),
            "split_recovered": 0,
            "final_review": {
                "mode": "apply",
                "findings": list(findings or []),
                "applied": [],
                "skipped": list(findings or []),
            },
        }

    def test_passes_clean_transcript(self) -> None:
        report = evaluate_minutes_quality(
            text="自然な本文です。",
            readable_stats=self._stats(),
        )
        self.assertEqual(report["status"], "pass")

    def test_blocks_failed_chunk_and_high_finding(self) -> None:
        report = evaluate_minutes_quality(
            text="崩れた本文。",
            readable_stats=self._stats(
                failed=[1],
                findings=[
                    {
                        "confidence": "high",
                        "quote": "崩れた本文",
                        "fix": "",
                    }
                ],
            ),
        )
        codes = {x["code"] for x in report["blockers"]}
        self.assertIn("readable_chunk_fallback", codes)
        self.assertIn("final_review_unresolved", codes)

    def test_blocks_remaining_verify_tag(self) -> None:
        report = evaluate_minutes_quality(
            text="名称[要確認]です。",
            readable_stats=self._stats(),
        )
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["metrics"]["verify_tag_count"], 1)

    def test_blocks_confirmed_pair_still_present(self) -> None:
        report = evaluate_minutes_quality(
            text="多分セントに依頼します。",
            readable_stats=self._stats(),
            correction_audit_rows=[
                {
                    "wrong": "セント",
                    "correct": "アクセンチュア",
                    "status": "applied",
                }
            ],
        )
        self.assertIn(
            "confirmed_corrections_unapplied",
            {x["code"] for x in report["blockers"]},
        )

    def test_blocks_only_pending_unknowns_still_present_in_final_text(self) -> None:
        pending = [{"status": "open", "text": "重複したと思いますと思います"}]
        blocked = evaluate_minutes_quality(
            text="重複したと思いますと思います",
            readable_stats=self._stats(),
            unknown_points=pending,
        )
        self.assertIn(
            "pending_unknowns_remaining",
            {x["code"] for x in blocked["blockers"]},
        )
        stale = evaluate_minutes_quality(
            text="重複を解消したと思います",
            readable_stats=self._stats(),
            unknown_points=pending,
        )
        self.assertEqual(stale["status"], "pass")

    def test_enforce_raises_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"MINUTES_QUALITY_GATE_MODE": "enforce"},
                clear=False,
            ):
                with self.assertRaises(MinutesQualityGateError):
                    run_minutes_quality_gate(
                        job_dir=tmp,
                        text="名称[要確認]",
                        readable_stats=self._stats(),
                    )
            payload = json.loads(
                (Path(tmp) / "minutes_quality_gate.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(payload["status"], "blocked")

    def test_unresolved_final_finding_is_queued_for_question_cycle(self) -> None:
        finding = {
            "confidence": "medium",
            "quote": "意味不明の断片です",
            "issue": "文脈上確定できない",
            "fix": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            count = _queue_unresolved_final_findings(
                job_dir=tmp,
                text="前文。意味不明の断片です。後文。",
                readable_stats=self._stats(findings=[finding]),
            )
            points = json.loads(
                (Path(tmp) / "unknown_points.json").read_text(encoding="utf-8")
            )
        self.assertEqual(count, 1)
        self.assertEqual(points[0]["source"], "final_review")
        self.assertEqual(points[0]["status"], "open")


if __name__ == "__main__":
    unittest.main()
