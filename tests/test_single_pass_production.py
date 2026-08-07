from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from detect_unknown_points import extract_single_pass_uncertainties
from single_pass_independent_verifier import (
    apply_deterministic_verifier_repairs,
    verify_and_repair_until_stable,
    verifier_findings_to_unknowns,
)


class SinglePassUncertaintyTests(unittest.TestCase):
    def test_extracts_inline_uncertainties_without_duplicates(self) -> None:
        text = (
            "前半です。[要確認: 原文『地下16』]続き。"
            "もう一度[要確認：原文「地下16」]。"
        )
        items = extract_single_pass_uncertainties(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["anomaly_word"], "地下16")
        self.assertEqual(items[0]["source"], "single_pass_editor")


class IndependentVerifierRepairTests(unittest.TestCase):
    def test_question_required_unreadable_warning_is_promoted(self) -> None:
        from single_pass_independent_verifier import (
            verify_single_pass_transcript,
        )

        payload = {
            "status": "pass",
            "findings": [
                {
                    "severity": "warning",
                    "type": "unreadable",
                    "raw_quote": "raw",
                    "edited_quote": "broken",
                    "issue": "意味を取れない",
                    "question_needed": True,
                    "hypothesis": "",
                    "replacement": "",
                }
            ],
            "summary": "",
        }
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                payload, ensure_ascii=False
                            ),
                        }
                    ]
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp, patch(
            "single_pass_independent_verifier."
            "resolve_openai_api_key",
            return_value=("key", "test"),
        ), patch(
            "single_pass_independent_verifier.requests.post",
            return_value=response,
        ):
            report = verify_single_pass_transcript(
                raw_text="raw",
                edited_text="broken",
                job_dir=Path(tmp),
            )
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            report["findings"][0]["severity"], "blocker"
        )

    def test_applies_exact_unique_non_question_repair(self) -> None:
        report = {
            "findings": [
                {
                    "severity": "blocker",
                    "question_needed": False,
                    "edited_quote": "Pが70以上",
                    "replacement": "Pが70以下",
                    "issue": "方向が反転",
                }
            ]
        }
        output, applied = apply_deterministic_verifier_repairs(
            "基準はPが70以上です。", report
        )
        self.assertEqual(output, "基準はPが70以下です。")
        self.assertEqual(len(applied), 1)

    def test_does_not_fuzzy_or_global_replace(self) -> None:
        report = {
            "findings": [
                {
                    "severity": "blocker",
                    "question_needed": False,
                    "edited_quote": "移動",
                    "replacement": "異動",
                    "issue": "一箇所のみ",
                }
            ]
        }
        output, applied = apply_deterministic_verifier_repairs(
            "部署の移動。机の移動。", report
        )
        self.assertEqual(output, "部署の移動。机の移動。")
        self.assertEqual(applied, [])

    def test_blockers_convert_to_existing_question_schema(self) -> None:
        unknowns = verifier_findings_to_unknowns(
            {
                "findings": [
                    {
                        "severity": "blocker",
                        "edited_quote": "2対1",
                        "issue": "原文では1対1の可能性",
                        "question_needed": True,
                        "hypothesis": "1対1",
                    },
                    {
                        "severity": "warning",
                        "edited_quote": "軽微",
                        "issue": "文体",
                    },
                ]
            }
        )
        self.assertEqual(len(unknowns), 1)
        self.assertEqual(
            unknowns[0]["source"],
            "single_pass_independent_verifier",
        )

    def test_repeats_repairs_until_verified_fixed_point(self) -> None:
        reports = [
            {
                "status": "pass",
                "findings": [
                    {
                        "severity": "warning",
                        "question_needed": False,
                        "edited_quote": "A",
                        "replacement": "B",
                        "issue": "first",
                    }
                ],
            },
            {
                "status": "blocked",
                "findings": [
                    {
                        "severity": "blocker",
                        "question_needed": False,
                        "edited_quote": "B",
                        "replacement": "C",
                        "issue": "second",
                    }
                ],
            },
            {"status": "pass", "findings": []},
        ]
        with patch(
            "single_pass_independent_verifier."
            "verify_single_pass_transcript",
            side_effect=reports,
        ) as verifier:
            text, report, repairs = verify_and_repair_until_stable(
                raw_text="raw",
                edited_text="A",
                job_dir=Path("."),
            )
        self.assertEqual(text, "C")
        self.assertEqual(report["status"], "pass")
        self.assertEqual(len(repairs), 2)
        self.assertEqual(verifier.call_count, 3)


class GenerateSinglePassFinalTests(unittest.TestCase):
    def test_regenerates_from_raw_and_writes_canonical_after_qa(self) -> None:
        from generate_minutes_transcript import _generate_single_pass_final

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "merged_transcript.txt").write_text(
                "えっと、Pは70以下です。",
                encoding="utf-8",
            )
            editor_meta = {
                "complete": True,
                "model": "claude-opus-5",
            }
            pass_report = {
                "status": "pass",
                "findings": [],
                "model": "gpt-4.1",
            }
            with patch(
                "shadow_single_pass_editor.edit_transcript_once",
                return_value=("Pは70以下です。", editor_meta),
            ), patch(
                "single_pass_independent_verifier."
                "verify_and_repair_until_stable",
                return_value=(
                    "Pは70以下です。",
                    pass_report,
                    [],
                ),
            ), patch(
                "single_pass_independent_verifier.write_verifier_report"
            ):
                text, source, readable, stats = (
                    _generate_single_pass_final(str(job_dir))
                )
            self.assertEqual(text, "Pは70以下です。")
            self.assertTrue(readable)
            self.assertTrue(stats["single_pass_primary"])
            self.assertEqual(stats["final_review"]["findings"], [])
            self.assertEqual(
                (
                    job_dir / "merged_transcript_after_qa.txt"
                ).read_text(encoding="utf-8"),
                "Pは70以下です。\n",
            )
            self.assertTrue(source.endswith("merged_transcript.txt"))


if __name__ == "__main__":
    unittest.main()
