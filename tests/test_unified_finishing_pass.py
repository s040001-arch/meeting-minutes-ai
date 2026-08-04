from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from unified_finishing_pass import (
    _split_windows,
    reflow_long_paragraphs,
    run_unified_finishing,
)


def _no_resolver(*, text, findings, meeting_profile=None, force=False):
    return text, [], []


class UnifiedFinishingPassTests(unittest.TestCase):
    def _run(self, text, audit_findings, verify_findings, resolver=_no_resolver):
        calls = {"reviewer": []}

        def fake_reviewer(window_text, meeting_profile=None):
            calls["reviewer"].append(window_text)
            # 監査（窓ごと）と検証（全文1回）を呼び出し順で区別する。
            if len(calls["reviewer"]) <= len(_split_windows(text)):
                return [f for f in audit_findings if f["quote"] in window_text]
            return verify_findings

        with tempfile.TemporaryDirectory() as job_dir:
            with patch.dict(
                os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False
            ):
                with patch(
                    "final_review_pass._call_reviewer",
                    side_effect=fake_reviewer,
                ), patch(
                    "editorial_transcript_pass.resolve_reader_blocking_findings",
                    side_effect=resolver,
                ):
                    out, stats, report = run_unified_finishing(
                        job_dir=job_dir,
                        text=text,
                        meeting_profile={"participants": ["土井様"]},
                    )
            report_path = os.path.join(job_dir, "final_review_report.json")
            written = json.loads(open(report_path, encoding="utf-8").read())
        return out, stats, report, written, calls

    def test_confident_fix_is_applied_pinpoint_and_verified(self) -> None:
        text = (
            "研修の対象は88kgの機材を扱う担当者です。\n\n"
            "承認にそれぞれ同じ調査を行い、差分をリフト値とします。"
        )
        audit = [
            {
                "type": "unnatural",
                "quote": "承認にそれぞれ同じ調査を行い",
                "issue": "文脈上「両群にそれぞれ」の誤認識",
                "fix": "両群にそれぞれ同じ調査を行い",
                "confidence": "high",
            }
        ]
        out, stats, report, written, _ = self._run(text, audit, [])
        self.assertIn("両群にそれぞれ同じ調査を行い", out)
        self.assertNotIn("承認にそれぞれ", out)
        self.assertIn("88kg", out)
        self.assertFalse(stats["failed"])
        self.assertEqual(len(report["applied"]), 1)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["mode"], "apply")
        self.assertTrue(written["unified"])

    def test_unfixable_issue_remains_as_finding_for_question(self) -> None:
        text = "山口部長の話です。\n\n重要な決定がありました。"
        finding = {
            "type": "notation",
            "quote": "山口部長の話です。",
            "issue": "人名ゆれの疑い（川口/山口）",
            "fix": "",
            "confidence": "medium",
        }
        out, stats, report, _written, _ = self._run(text, [finding], [finding])
        self.assertEqual(out.strip(), text.strip())
        self.assertEqual(report["applied"], [])
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(
            report["findings"][0]["quote"], "山口部長の話です。"
        )

    def test_audit_window_failure_is_fail_closed(self) -> None:
        def broken_reviewer(window_text, meeting_profile=None):
            raise RuntimeError("api down")

        with tempfile.TemporaryDirectory() as job_dir:
            with patch.dict(
                os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False
            ):
                with patch(
                    "final_review_pass._call_reviewer",
                    side_effect=broken_reviewer,
                ):
                    _out, stats, report = run_unified_finishing(
                        job_dir=job_dir,
                        text="本文です。",
                        meeting_profile={},
                    )
        self.assertTrue(stats["failed"])
        self.assertIn("audit_windows_failed", str(report.get("error")))

    def test_fix_changing_numbers_is_not_auto_applied(self) -> None:
        text = "納期は3月10日です。\n\n以上です。"
        audit = [
            {
                "type": "unnatural",
                "quote": "納期は3月10日です。",
                "issue": "日付の疑い",
                "fix": "納期は3月20日です。",
                "confidence": "high",
            }
        ]
        out, _stats, report, _written, _ = self._run(text, audit, audit)
        self.assertIn("3月10日", out)
        self.assertEqual(report["applied"], [])
        self.assertEqual(len(report["findings"]), 1)

    def test_verify_round_fixes_are_confirmed_by_final_verify(self) -> None:
        text = "冒頭の説明です。\n\n結論の説明です。"
        verify_finding = {
            "type": "unnatural",
            "quote": "冒頭の説明です。",
            "issue": "「冒頭の」が誤認識",
            "fix": "最初の説明です。",
            "confidence": "high",
        }
        stage = {"n": 0}

        def fake_reviewer(window_text, meeting_profile=None):
            stage["n"] += 1
            if stage["n"] == 1:  # 監査（窓1つ）: 問題なし
                return []
            if stage["n"] == 2:  # 検証1回目: 修正可能な問題を発見
                return [verify_finding]
            return []  # 最終検証: 問題なし

        with tempfile.TemporaryDirectory() as job_dir:
            with patch.dict(
                os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False
            ):
                with patch(
                    "final_review_pass._call_reviewer",
                    side_effect=fake_reviewer,
                ), patch(
                    "editorial_transcript_pass.resolve_reader_blocking_findings",
                    side_effect=_no_resolver,
                ):
                    out, stats, report = run_unified_finishing(
                        job_dir=job_dir,
                        text=text,
                        meeting_profile={},
                    )
        self.assertIn("最初の説明です。", out)
        self.assertEqual(report["findings"], [])
        self.assertEqual(len(report["applied"]), 1)
        # 監査 + 検証1回目 + 最終検証 = 3回のレビュー呼び出し
        self.assertEqual(stage["n"], 3)
        self.assertFalse(stats["failed"])

    def test_reflow_splits_walls_without_changing_characters(self) -> None:
        sentence = "これは長い説明の文であり内容を保持したまま分割されるべきです。"
        wall = sentence * 40
        reflowed = reflow_long_paragraphs(wall)
        self.assertGreater(reflowed.count("\n\n"), 0)
        self.assertEqual(
            reflowed.replace("\n", ""), wall.replace("\n", "")
        )

    def test_long_text_is_audited_in_multiple_windows(self) -> None:
        paragraph = "これは会議の発言内容です。" + "詳細な説明が続きます。" * 30
        text = "\n\n".join(paragraph for _ in range(40))
        windows = _split_windows(text)
        self.assertGreater(len(windows), 1)
        self.assertEqual(
            "".join(windows).replace("\n", ""), text.replace("\n", "")
        )


class ReadableTranscriptUnifiedWiringTests(unittest.TestCase):
    def test_unified_mode_bypasses_chunk_rewrites(self) -> None:
        from readable_transcript import generate_readable_transcript_with_stats

        def fake_unified(*, job_dir, text, meeting_profile=None):
            stats = {"enabled": True, "failed": False, "auto_applied": 2}
            report = {"mode": "apply", "unified": True, "findings": []}
            return "統合仕上げ済み本文\n", stats, report

        with tempfile.TemporaryDirectory() as job_dir:
            with patch.dict(
                os.environ, {"UNIFIED_FINISHING_ENABLED": "1"}, clear=False
            ):
                with patch(
                    "unified_finishing_pass.run_unified_finishing",
                    side_effect=fake_unified,
                ), patch(
                    "readable_transcript.polish_transcript_text_with_stats",
                    side_effect=AssertionError(
                        "legacy chunk rewrite must not run in unified mode"
                    ),
                ):
                    text, stats, out_path = (
                        generate_readable_transcript_with_stats(
                            job_dir=job_dir,
                            source_text="元テキスト",
                            meeting_profile={},
                        )
                    )
            self.assertEqual(text, "統合仕上げ済み本文\n")
            self.assertTrue(stats["final_review"]["unified"])
            self.assertEqual(stats["total_chunks"], 0)
            with open(out_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "統合仕上げ済み本文\n")

    def test_unified_error_report_blocks_via_gate_shape(self) -> None:
        from minutes_quality_gate import evaluate_minutes_quality

        result = evaluate_minutes_quality(
            text="本文",
            readable_stats={
                "total_chunks": 0,
                "failed_chunk_idx": [],
                "final_review": {
                    "mode": "apply",
                    "unified": True,
                    "error": "audit_windows_failed:...",
                    "findings": [],
                    "applied": [],
                },
            },
        )
        self.assertEqual(result["status"], "blocked")
        codes = [b["code"] for b in result["blockers"]]
        self.assertIn("final_review_error", codes)

    def test_unified_remaining_finding_blocks_and_queues(self) -> None:
        from minutes_quality_gate import evaluate_minutes_quality

        result = evaluate_minutes_quality(
            text="山口部長の話です。",
            readable_stats={
                "total_chunks": 0,
                "failed_chunk_idx": [],
                "final_review": {
                    "mode": "apply",
                    "unified": True,
                    "findings": [
                        {
                            "type": "notation",
                            "quote": "山口部長の話です。",
                            "issue": "人名ゆれの疑い",
                            "fix": "",
                            "confidence": "medium",
                        }
                    ],
                    "applied": [],
                },
            },
        )
        self.assertEqual(result["status"], "blocked")
        codes = [b["code"] for b in result["blockers"]]
        self.assertIn("final_review_unresolved", codes)


if __name__ == "__main__":
    unittest.main()
