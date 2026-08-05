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

    def test_blocks_failed_editorial_transcript(self) -> None:
        stats = self._stats()
        stats["editorial_transcript"] = {
            "enabled": True,
            "attempted": True,
            "applied": False,
            "failed": True,
            "validation_errors": ["numeric_tokens_changed"],
        }
        report = evaluate_minutes_quality(
            text="本文",
            readable_stats=stats,
        )
        self.assertIn(
            "editorial_transcript_failed",
            {x["code"] for x in report["blockers"]},
        )

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

    def test_low_reader_blocking_garble_blocks_and_is_queued(self) -> None:
        # 読者の理解を妨げる崩れは、確信度lowでも「残したまま公開」しない。
        # 自動修復できなければ質問として送る。
        finding = {
            "type": "fragment",
            "confidence": "low",
            "quote": "見学者タイトルを回ってる",
            "issue": "意味不明な崩れ断片",
            "fix": "",
        }
        report = evaluate_minutes_quality(
            text="前文。見学者タイトルを回ってる。後文。",
            readable_stats=self._stats(findings=[finding]),
        )
        self.assertIn(
            "final_review_unresolved",
            {x["code"] for x in report["blockers"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            count = _queue_unresolved_final_findings(
                job_dir=tmp,
                text="前文。見学者タイトルを回ってる。後文。",
                readable_stats=self._stats(findings=[finding]),
            )
        self.assertEqual(count, 1)

    def test_low_minor_wording_also_blocks_and_is_queued(self) -> None:
        # ユーザー方針（2026-08-05）: 低確信でも「記録だけで放置」しない。
        # 修正段で直せなかった残存問題は確信度によらず質問へ回す。
        finding = {
            "type": "unnatural",
            "confidence": "low",
            "quote": "食べさせたらやってくれる",
            "issue": "口語表現の表記が前後と不統一の軽微な違和感",
            "fix": "",
        }
        report = evaluate_minutes_quality(
            text="前文。食べさせたらやってくれる。後文。",
            readable_stats=self._stats(findings=[finding]),
        )
        self.assertIn(
            "final_review_unresolved",
            {x["code"] for x in report["blockers"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            count = _queue_unresolved_final_findings(
                job_dir=tmp,
                text="前文。食べさせたらやってくれる。後文。",
                readable_stats=self._stats(findings=[finding]),
            )
        self.assertEqual(count, 1)

    def test_stale_pre_readable_unknown_is_closed_when_queueing(self) -> None:
        finding = {
            "confidence": "medium",
            "quote": "新しい不明箇所",
            "issue": "確認が必要",
            "fix": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "unknown_points.json").write_text(
                json.dumps(
                    [
                        {
                            "status": "open",
                            "source": "final_review",
                            "text": "既に整文で消えた長い断片",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _queue_unresolved_final_findings(
                job_dir=tmp,
                text="本文。新しい不明箇所。",
                readable_stats=self._stats(findings=[finding]),
            )
            points = json.loads(
                (Path(tmp) / "unknown_points.json").read_text(encoding="utf-8")
            )
        self.assertEqual(points[0]["status"], "resolved")
        self.assertEqual(points[0]["resolved_via"], "final_readable_text")

    def test_abstract_claude_question_is_not_closed_by_literal_search(self) -> None:
        finding = {
            "confidence": "medium",
            "quote": "新しい不明箇所",
            "issue": "確認が必要",
            "fix": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "unknown_points.json").write_text(
                json.dumps(
                    [
                        {
                            "status": "open",
                            "source": "claude_step9",
                            "text": "体重推移の数値が矛盾している",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _queue_unresolved_final_findings(
                job_dir=tmp,
                text="本文。新しい不明箇所。",
                readable_stats=self._stats(findings=[finding]),
            )
            points = json.loads(
                (Path(tmp) / "unknown_points.json").read_text(encoding="utf-8")
            )
        self.assertEqual(points[0]["status"], "open")


if __name__ == "__main__":
    unittest.main()
