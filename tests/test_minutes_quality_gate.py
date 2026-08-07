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
                        "issue": "意味不明な崩れ",
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
        pending = [
            {
                "status": "open",
                "source": "coherence_review",
                "text": "重複したと思いますと思います",
            }
        ]
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

    def test_content_vagueness_unknowns_warn_but_do_not_block(self) -> None:
        # 「主語が曖昧」「数値が不明確」等は発言そのものの曖昧さ。
        # 質問選択器が価値判断でスキップするため、ブロックすると
        # 「質問しないのにゲートが塞ぐ」デッドロックになる（2026-08-05）。
        pending = [
            {
                "status": None,
                "type": "主語",
                "text": "こう来た人に対して対応することが多い",
                "reason": "実施主体が明示されていません。",
            },
            {
                "status": None,
                "type": "数値",
                "text": "もう2ヶ月で行きたいなと思って",
                "reason": "定量情報が不明確です。",
            },
        ]
        report = evaluate_minutes_quality(
            text="こう来た人に対して対応することが多い。もう2ヶ月で行きたいなと思って。",
            readable_stats=self._stats(),
            unknown_points=pending,
        )
        self.assertEqual(report["status"], "pass")
        codes = {x["code"] for x in report["warnings"]}
        self.assertIn("content_vagueness_unasked", codes)

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
            "issue": "意味不明な崩れで文脈上確定できない",
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

    def test_minor_wording_warns_but_does_not_block_or_queue(self) -> None:
        # ユーザー方針（2026-08-06改訂）: 読めば理解できる文体的違和感は
        # 質問・ブロックしない（楽天ジョブの質問洪水の再発防止）。
        # warning として記録は残す。
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
        self.assertNotIn(
            "final_review_unresolved",
            {x["code"] for x in report["blockers"]},
        )
        self.assertIn(
            "minor_wording_unresolved",
            {x["code"] for x in report["warnings"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            count = _queue_unresolved_final_findings(
                job_dir=tmp,
                text="前文。食べさせたらやってくれる。後文。",
                readable_stats=self._stats(findings=[finding]),
            )
        self.assertEqual(count, 0)

    def test_covered_finding_converts_block_to_warning(self) -> None:
        # 2026-08-07: 回答済み領域と重なる再検出は「同じ内容の質問」に
        # なるため質問せず、warning として記録し公開を許す（固定点終了）。
        finding = {
            "type": "fragment",
            "confidence": "medium",
            "quote": "意味不明の断片ですがユーザー回答済みの箇所",
            "issue": "意味不明な崩れ",
            "fix": "",
        }
        text = "前文。意味不明の断片ですがユーザー回答済みの箇所。後文。"
        blocked = evaluate_minutes_quality(
            text=text,
            readable_stats=self._stats(findings=[finding]),
            covered_surfaces=[],
        )
        self.assertIn(
            "final_review_unresolved",
            {x["code"] for x in blocked["blockers"]},
        )
        allowed = evaluate_minutes_quality(
            text=text,
            readable_stats=self._stats(findings=[finding]),
            covered_surfaces=["意味不明の断片ですがユーザー回答済みの箇所"],
        )
        self.assertNotIn(
            "final_review_unresolved",
            {x["code"] for x in allowed["blockers"]},
        )
        self.assertIn(
            "already_covered_by_answers",
            {x["code"] for x in allowed["warnings"]},
        )

    def test_covered_pending_unknowns_do_not_block(self) -> None:
        pending = [
            {
                "status": "open",
                "source": "final_review",
                "text": "残っている崩れ断片の該当箇所",
            }
        ]
        report = evaluate_minutes_quality(
            text="本文。残っている崩れ断片の該当箇所。",
            readable_stats=self._stats(),
            unknown_points=pending,
            covered_surfaces=["残っている崩れ断片の該当箇所"],
        )
        self.assertEqual(report["status"], "pass")
        self.assertIn(
            "pending_unknowns_covered_by_answers",
            {x["code"] for x in report["warnings"]},
        )

    def test_collect_covered_surfaces_from_answered_points(self) -> None:
        # 回答済み unknown_points の引用スパンが照合リストに入ること。
        from minutes_quality_gate import collect_covered_surfaces

        with tempfile.TemporaryDirectory() as tmp:
            surfaces = collect_covered_surfaces(
                tmp,
                [
                    {
                        "status": "answered",
                        "text": "回答済みの引用スパンがこちらです",
                        "answer": "はい",
                    },
                    {
                        "status": "open",
                        "text": "未回答の引用スパンは含めない",
                    },
                ],
            )
        self.assertIn("回答済みの引用スパンがこちらです", surfaces)
        self.assertNotIn("未回答の引用スパンは含めない", surfaces)

    def test_generated_stale_resolution_is_not_authoritative(self) -> None:
        from minutes_quality_gate import collect_covered_surfaces

        stale = {
            "status": "resolved",
            "resolved_via": "final_readable_text",
            "text": "再生成で再発し得る崩れの引用スパン",
        }
        self.assertNotIn(
            stale["text"],
            collect_covered_surfaces("", [stale]),
        )

    def test_reopens_stale_generated_resolution_for_verifier(self) -> None:
        quote = "ホームページを更新すると最新版が出て記憶する"
        existing = [
            {
                "type": "final_review",
                "source": "final_review",
                "anomaly_id": "old",
                "text": quote,
                "span_text": quote,
                "status": "resolved",
                "resolved_via": "final_readable_text",
            }
        ]
        finding = {
            "type": "contradiction",
            "confidence": "high",
            "quote": quote,
            "issue": "意味を取れない",
            "fix": "",
            "source": "single_pass_independent_verifier",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unknown_points.json"
            path.write_text(
                json.dumps(existing, ensure_ascii=False),
                encoding="utf-8",
            )
            count = _queue_unresolved_final_findings(
                job_dir=tmp,
                text=f"前文。{quote}。後文。",
                readable_stats=self._stats(findings=[finding]),
            )
            points = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(count, 1)
        self.assertEqual(points[0]["status"], "open")
        self.assertNotIn("resolved_via", points[0])
        self.assertEqual(
            points[0]["source"],
            "single_pass_independent_verifier",
        )

    def test_overlapping_quote_does_not_create_duplicate(self) -> None:
        # 2026-08-06: 再監査が同じ箇所を微妙に違う引用範囲で返しても、
        # 既存項目を更新するだけで新規項目を増やさない。
        text = "前文。なんか僕も森さんにこう見られて配属が決まった経験があるんで。後文。"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "unknown_points.json").write_text(
                json.dumps(
                    [
                        {
                            "status": "open",
                            "source": "final_review",
                            "anomaly_id": "final_aaa",
                            "text": "なんか僕も森さんにこう見られて配属が決まった経験があるんで",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _queue_unresolved_final_findings(
                job_dir=tmp,
                text=text,
                readable_stats=self._stats(
                    findings=[
                        {
                            "confidence": "medium",
                            "quote": "僕も森さんにこう見られて配属が決まった経験があるんで",
                            "issue": "意味不明（見られて→誤認識の疑い）",
                            "fix": "",
                        }
                    ]
                ),
            )
            points = json.loads(
                (Path(tmp) / "unknown_points.json").read_text(encoding="utf-8")
            )
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["anomaly_id"], "final_aaa")

    def test_answered_overlapping_quote_is_not_requeued(self) -> None:
        # 回答済みの箇所を再監査が別範囲で再検出しても、聞き直さない。
        text = "前文。僕も森さんにこう見られて配属が決まった経験があるんで。後文。"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "unknown_points.json").write_text(
                json.dumps(
                    [
                        {
                            "status": "answered",
                            "source": "final_review",
                            "anomaly_id": "final_bbb",
                            "text": "なんか僕も森さんにこう見られて配属が決まった経験があるんで",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            count = _queue_unresolved_final_findings(
                job_dir=tmp,
                text=text,
                readable_stats=self._stats(
                    findings=[
                        {
                            "confidence": "medium",
                            "quote": "僕も森さんにこう見られて配属が決まった経験があるんで",
                            "issue": "意味不明（誤認識の疑い）",
                            "fix": "",
                        }
                    ]
                ),
            )
            points = json.loads(
                (Path(tmp) / "unknown_points.json").read_text(encoding="utf-8")
            )
        self.assertEqual(count, 0)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["status"], "answered")

    def test_stale_pre_readable_unknown_is_closed_when_queueing(self) -> None:
        finding = {
            "confidence": "medium",
            "quote": "新しい不明箇所",
            "issue": "意味不明で確認が必要",
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
