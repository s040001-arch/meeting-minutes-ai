"""確定修正の集約・強制適用と関連ゲート/スパン修正のテスト（2026-08-05）。"""
import json
import os
import tempfile
import unittest


class ConfirmedCorrectionsTests(unittest.TestCase):
    def _write_jsonl(self, path: str, rows: list[dict]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_collects_from_all_sources(self) -> None:
        from confirmed_corrections import collect_confirmed_pairs

        with tempfile.TemporaryDirectory() as tmp:
            self._write_jsonl(
                os.path.join(tmp, "line_correction_audit.jsonl"),
                [{"wrong": "湯でみ", "correct": "Udemy"}],
            )
            self._write_jsonl(
                os.path.join(tmp, "batch_corrections_audit.jsonl"),
                [
                    {
                        "applied": [
                            {
                                "before": "山谷さん",
                                "after": "山屋さん",
                                "action": "correct",
                            },
                            # keep 操作は置換ペアにしない
                            {
                                "before": "何か[要確認]",
                                "after": "何か",
                                "action": "keep",
                            },
                        ]
                    }
                ],
            )
            self._write_jsonl(
                os.path.join(tmp, "auto_triage_audit.jsonl"),
                [{"applied": [{"word": "習字", "after": "週次"}]}],
            )
            self._write_jsonl(
                os.path.join(tmp, "knowledge_self_answer_audit.jsonl"),
                [{"wrong": "三木谷さん", "right": "三木谷社長"}],
            )
            with open(
                os.path.join(tmp, "auto_corrections.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    [{"before": "味ンダー", "after": "アジェンダ", "action": "correct"}],
                    f,
                    ensure_ascii=False,
                )
            pairs = collect_confirmed_pairs(tmp)
            got = {p["wrong"]: p["right"] for p in pairs}
            self.assertEqual(got["湯でみ"], "Udemy")
            self.assertEqual(got["山谷さん"], "山屋さん")
            self.assertEqual(got["習字"], "週次")
            self.assertEqual(got["味ンダー"], "アジェンダ")
            self.assertNotIn("何か[要確認]", got)

    def test_unsafe_pairs_are_excluded(self) -> None:
        from confirmed_corrections import collect_confirmed_pairs

        with tempfile.TemporaryDirectory() as tmp:
            self._write_jsonl(
                os.path.join(tmp, "line_correction_audit.jsonl"),
                [
                    # wrong が right の部分文字列 → 発散するので除外
                    {"wrong": "山屋", "correct": "山屋さん"},
                    # 1文字 → 除外
                    {"wrong": "は", "correct": "が"},
                    # 文まるごと → 除外
                    {
                        "wrong": "これは長い文です。続きもある。",
                        "correct": "直した文です。",
                    },
                ],
            )
            self.assertEqual(collect_confirmed_pairs(tmp), [])

    def test_enforce_replaces_all_occurrences(self) -> None:
        from confirmed_corrections import enforce_confirmed_pairs

        text = "山谷さんに聞く。山谷さんの担当。湯でみで学ぶ。"
        pairs = [
            {"wrong": "山谷さん", "right": "山屋さん", "source": "batch_answer"},
            {"wrong": "湯でみ", "right": "Udemy", "source": "line_answer"},
            {"wrong": "存在しない語", "right": "何か", "source": "x"},
        ]
        out, enforced = enforce_confirmed_pairs(text, pairs)
        self.assertEqual(out, "山屋さんに聞く。山屋さんの担当。Udemyで学ぶ。")
        self.assertEqual(len(enforced), 2)
        self.assertEqual(enforced[0]["count"], 2)


class GateAiCorrectionFallbackTests(unittest.TestCase):
    def _stats(self) -> dict:
        return {
            "total_chunks": 1,
            "failed_chunk_idx": [],
            "split_recovered": 0,
            "final_review": {
                "mode": "apply",
                "findings": [],
                "applied": [],
                "skipped": [],
            },
        }

    def test_ai_correction_fallback_blocks(self) -> None:
        from minutes_quality_gate import evaluate_minutes_quality

        report = evaluate_minutes_quality(
            text="本文です。",
            readable_stats=self._stats(),
            ai_correction_meta={
                "used_fallback": True,
                "fallback_reason": "exception:overloaded_error",
            },
        )
        self.assertEqual(report["status"], "blocked")
        self.assertIn(
            "ai_correction_fallback",
            {b["code"] for b in report["blockers"]},
        )

    def test_ai_correction_success_passes(self) -> None:
        from minutes_quality_gate import evaluate_minutes_quality

        report = evaluate_minutes_quality(
            text="本文です。",
            readable_stats=self._stats(),
            ai_correction_meta={"used_fallback": False},
        )
        self.assertEqual(report["status"], "pass")


class SpanSentenceBugTests(unittest.TestCase):
    def test_sentence_span_corrected_does_not_duplicate_text(self) -> None:
        # 2026-08-05 バグ再現: span_corrected が文全体の修正版のとき、
        # 単語位置に文を差し込むと本文が二重化していた。
        from span_correction import apply_span_correction_from_anomaly

        text = "御社はプレサナの研修を受けています。次の話。"
        anomaly = {
            "auto_fixable": True,
            "anomaly_word": "プレサナ",
            "estimated_correction": "プレセナ",
            "span_text": "御社はプレサナの研修を受けています。",
            "span_corrected": "御社はプレセナの研修を受けています。",
            "span_start": text.find("プレサナ"),
        }
        out, entry = apply_span_correction_from_anomaly(text, anomaly)
        self.assertEqual(out, "御社はプレセナの研修を受けています。次の話。")
        self.assertIsNotNone(entry)
        # 二重化していないこと
        self.assertEqual(out.count("研修を受けています"), 1)

    def test_sentence_only_falls_back_to_span_replacement(self) -> None:
        from span_correction import apply_span_correction_from_anomaly

        text = "前文。壊れた断片がここにある。後文。"
        anomaly = {
            "auto_fixable": True,
            "anomaly_word": "壊れた断片",
            "estimated_correction": "",
            "span_text": "壊れた断片がここにある。",
            "span_corrected": "正しい文がここにある。",
            "span_start": text.find("壊れた断片"),
        }
        out, entry = apply_span_correction_from_anomaly(text, anomaly)
        self.assertEqual(out, "前文。正しい文がここにある。後文。")
        self.assertEqual(entry["mode"], "span_replace")


if __name__ == "__main__":
    unittest.main()
