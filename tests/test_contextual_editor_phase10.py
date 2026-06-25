"""Tests for Phase 10 edit proposal schema and fact guards."""
from __future__ import annotations

import unittest

from edit_proposal_schema import (
    FACT_NUMERIC,
    FACT_PROPER_NOUN,
    FACT_FILLER_GARBLE,
    VERDICT_ASK_WITH_CANDIDATE,
    VERDICT_ASK_WITHOUT_CANDIDATE,
    VERDICT_AUTO_CORRECT,
    VERDICT_AUTO_DELETE,
    align_proposal_spans_in_text,
    audit_garble_span,
    enforce_ambiguous_lexical_auto_correct,
    enforce_fact_routing,
    enforce_proper_noun_immutability,
    proper_noun_tokens_preserved,
    realign_span_after,
    remediate_empty_auto_correct,
    repair_span_after_from_correction,
    extract_correction_pair_from_reason,
    summarize_garble_audits,
    to_unknown_point,
)
from semantic_integrity_gate import extract_edit_context_pair, removed_beyond_garble, structural_delete_overreach, sync_delete_span_to_garble_fragment
from fact_classify import classify_fact_class, reclassify_proposal
from fact_integrity_gate import simulate_apply_proposals, verify_fact_integrity
from editor_apply import normalize_editor_delete_punctuation


class FactRoutingTests(unittest.TestCase):
    def test_numeric_blocks_auto_correct(self) -> None:
        p = enforce_fact_routing(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "fact_class": FACT_NUMERIC,
                "hypothesis": "",
                "span_before": "85万円",
            }
        )
        self.assertEqual(p["original_verdict"], VERDICT_AUTO_CORRECT)
        self.assertEqual(p["verdict"], VERDICT_ASK_WITHOUT_CANDIDATE)

    def test_proper_noun_with_hypothesis_becomes_ask_with_candidate(self) -> None:
        p = enforce_fact_routing(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "fact_class": FACT_PROPER_NOUN,
                "hypothesis": "盛岡",
                "span_before": "義理をか",
            }
        )
        self.assertEqual(p["verdict"], VERDICT_ASK_WITH_CANDIDATE)
        self.assertEqual(p["routing_override"], "fact_class_guard")

    def test_filler_garble_allows_auto_delete(self) -> None:
        p = enforce_fact_routing(
            {
                "verdict": VERDICT_AUTO_DELETE,
                "fact_class": "filler_garble",
                "span_before": "16時にちょっという",
            }
        )
        self.assertEqual(p["verdict"], VERDICT_AUTO_DELETE)


class FactClassifyTests(unittest.TestCase):
    def test_code_detects_numeric(self) -> None:
        fc, src = classify_fact_class(span_before="予算は85万円", llm_fact_class="lexical_fluency")
        self.assertEqual(fc, FACT_NUMERIC)
        self.assertEqual(src, "code_override")

    def test_garble_digit_not_numeric(self) -> None:
        fc, src = classify_fact_class(
            span_before="ちょっと 16 時にちょっという",
            llm_fact_class="filler_garble",
            llm_verdict="auto_delete",
        )
        self.assertEqual(fc, FACT_FILLER_GARBLE)
        self.assertNotEqual(fc, FACT_NUMERIC)

    def test_garble_llm_numeric_overridden(self) -> None:
        fc, _ = classify_fact_class(
            span_before="16 時にちょっという",
            llm_fact_class="numeric",
            llm_verdict="ask_without_candidate",
        )
        self.assertEqual(fc, FACT_FILLER_GARBLE)

    def test_participant_is_proper_noun(self) -> None:
        fc, _ = classify_fact_class(
            span_before="相原さんの件",
            llm_fact_class="lexical_fluency",
            meeting_profile={"participants": ["相原"]},
        )
        self.assertEqual(fc, FACT_PROPER_NOUN)

    def test_unlisted_unit_still_detected_as_numeric(self) -> None:
        """A garbled counter (車 standing in for 社) isn't in any fixed unit
        list, but the digit-adjacent-kanji structure still flags it as
        numeric instead of letting it slip through as lexical_fluency."""
        fc, src = classify_fact_class(
            span_before="イオンでも1000車ぐらい",
            span_after="イオンでも1000社ぐらい",
            llm_fact_class="lexical_fluency",
        )
        self.assertEqual(fc, FACT_NUMERIC)
        self.assertEqual(src, "code_override")

    def test_numeric_misclassified_as_lexical_fluency_blocks_auto_correct(self) -> None:
        """End-to-end: reclassify then route — must not stay auto_correct."""
        proposal = {
            "verdict": VERDICT_AUTO_CORRECT,
            "fact_class": "lexical_fluency",
            "span_before": "イオンでも1000車ぐらい",
            "span_after": "イオンでも1000社ぐらい",
            "hypothesis": "",
        }
        reclassify_proposal(proposal)
        enforce_fact_routing(proposal)
        self.assertEqual(proposal["fact_class"], FACT_NUMERIC)
        self.assertNotEqual(proposal["verdict"], VERDICT_AUTO_CORRECT)


class FactIntegrityGateTests(unittest.TestCase):
    def test_amounts_must_persist(self) -> None:
        before = "予算85万円と75万円の話。"
        after = "予算の話。"
        result = verify_fact_integrity(before, after)
        self.assertFalse(result.ok)
        self.assertTrue(any("amounts_missing" in v for v in result.violations))

    def test_schedule_must_persist(self) -> None:
        before = "2 年目の研修。3 人参加。"
        after = "研修。"
        result = verify_fact_integrity(before, after)
        self.assertFalse(result.ok)
        self.assertTrue(any("schedule_missing" in v for v in result.violations))

    def test_simulate_delete_preserves_schedule_elsewhere(self) -> None:
        before = "2 年目。あのーちょっと。3 人。"
        proposals = [
            {
                "verdict": VERDICT_AUTO_DELETE,
                "span_before": "あのーちょっと",
                "span_start": before.find("あのーちょっと"),
            }
        ]
        after = simulate_apply_proposals(before, proposals)
        result = verify_fact_integrity(before, after)
        self.assertTrue(result.ok)


class ProperNounImmutabilityTests(unittest.TestCase):
    def test_yokohama_trim_allowed(self) -> None:
        before = "あそこ横浜のやっぱ使うかなとうん処置しました"
        after = "あそこ横浜のやっぱ使うかなと"
        self.assertTrue(proper_noun_tokens_preserved(before, after, ["横浜"]))

    def test_place_rename_blocked(self) -> None:
        p = enforce_proper_noun_immutability(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "span_before": "あそこ横浜の話",
                "span_after": "あそこ仙台の話",
                "hypothesis": "",
            },
            meeting_profile={"participants": []},
            extra_place_names=["横浜", "仙台"],
        )
        self.assertEqual(p["verdict"], VERDICT_ASK_WITHOUT_CANDIDATE)
        self.assertEqual(p["routing_override"], "proper_noun_immutability_guard")


class UnknownPointDeriveTests(unittest.TestCase):
    def test_to_unknown_point_ask_without_candidate(self) -> None:
        up = to_unknown_point(
            {
                "proposal_id": "x",
                "verdict": VERDICT_ASK_WITHOUT_CANDIDATE,
                "span_before": "義理をか",
                "anomaly_word": "義理をか",
                "evidence": "八戸から義理をか",
                "importance": "地名文脈",
            }
        )
        self.assertEqual(up["source"], "contextual_editor")
        self.assertEqual(up["question_kind"], "without_candidate")


class GarbleSpanAuditTests(unittest.TestCase):
    def test_overdelete_span_flags_mismatch(self) -> None:
        audit = audit_garble_span(
            {
                "verdict": VERDICT_AUTO_DELETE,
                "span_before": "そもそも工程が多い。外いくつ",
                "garble_fragment": "外いくつ",
                "anomaly_word": "外いくつ",
                "evidence": "外いくつ。はい",
            }
        )
        self.assertFalse(audit["garble_fragment_match"])
        self.assertIn("span_garble_fragment_mismatch", audit["span_overflow_flags"])
        self.assertIn("span_prefix_beyond_garble_fragment", audit["span_overflow_flags"])

    def test_minimal_span_no_overflow_flags(self) -> None:
        audit = audit_garble_span(
            {
                "verdict": VERDICT_AUTO_DELETE,
                "span_before": "そこからさらに外いくつ",
                "garble_fragment": "そこからさらに外いくつ",
                "anomaly_word": "外いくつ",
                "evidence": "そこからさらに外いくつ。はい",
            }
        )
        self.assertTrue(audit["garble_fragment_match"])
        self.assertEqual(audit["span_overflow_flags"], [])

    def test_summarize_garble_audits(self) -> None:
        summary = summarize_garble_audits(
            [
                {
                    "verdict": VERDICT_AUTO_DELETE,
                    "span_before": "a",
                    "garble_fragment": "a",
                    "anomaly_word": "a",
                    "evidence": "a",
                },
                {
                    "verdict": VERDICT_AUTO_DELETE,
                    "span_before": "xx",
                    "garble_fragment": "x",
                    "anomaly_word": "x",
                    "evidence": "x",
                },
            ]
        )
        self.assertEqual(summary["auto_delete_count"], 2)
        self.assertEqual(summary["garble_fragment_match_count"], 1)
        self.assertEqual(summary["span_overflow_flag_count"], 1)


class SpanAlignmentTests(unittest.TestCase):
    def test_realign_span_after_seki(self) -> None:
        after = realign_span_after(
            "あの籍としては",
            "あの背景としては",
            "の籍としては",
        )
        self.assertEqual(after, "の背景としては")

    def test_align_seki_proposal(self) -> None:
        text = (
            "ま演習の内容になります。で、あ、の籍としては当然受講者の方がま研修に参加する前に。"
        )
        p = align_proposal_spans_in_text(
            text,
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "span_before": "あの籍としては",
                "span_after": "あの背景としては",
                "anomaly_word": "籍",
                "evidence": "の籍としては当然",
            },
        )
        self.assertGreaterEqual(p["span_start"], 0)
        self.assertEqual(p["span_before"], "の籍としては")
        self.assertEqual(p["span_after"], "の背景としては")
        self.assertEqual(
            text[p["span_start"] : p["span_start"] + len(p["span_before"])],
            p["span_before"],
        )

    def test_shrink_garble_fragment_subset(self) -> None:
        text = "主に今回あのロジシンの業務と、合っていますロジシンの業務の中の課題。"
        span = "ロジシンの業務と、合っていますロジシンの業務"
        start = text.find(span)
        p = align_proposal_spans_in_text(
            text,
            {
                "verdict": VERDICT_AUTO_DELETE,
                "span_before": span,
                "span_start": start,
                "garble_fragment": "、合っています",
                "anomaly_word": "合っています",
                "evidence": "、合っていますロジシン",
            },
        )
        self.assertEqual(p["span_before"], "、合っています")
        self.assertEqual(p["garble_fragment"], "、合っています")
        audit = audit_garble_span(p)
        self.assertTrue(audit["garble_fragment_match"])

    def test_garble_not_in_span_relocates(self) -> None:
        text = (
            "合っていますその研修の参加の目的の意識をこう醸成していくっていうところですね。"
        )
        p = align_proposal_spans_in_text(
            text,
            {
                "verdict": VERDICT_AUTO_DELETE,
                "span_before": "その研修の参加の目的の意識をこう醸成",
                "garble_fragment": "合っています",
                "anomaly_word": "合っています",
                "evidence": "合っていますその研修",
            },
        )
        self.assertIn("合っています", p["span_before"])
        self.assertIn(p["garble_fragment"], p["span_before"])
        self.assertEqual(p["span_before"], p["garble_fragment"])

    def test_leading_connector_stripped_from_redo_remnant(self) -> None:
        """④ must not eat a leading clause connector along with a redo remnant.

        「だからそれは大原あ。」のような④で、接続「だからそれは」は次の発話
        （大川社長から…）にかかる連結語なので残し、言い直し残骸「大原あ。」
        の核だけを削除範囲にする。
        """
        text = "いや、違うんすよ。だからそれは大原あ。大川社長からグループ会社レポート出してくれるってね。"
        p = align_proposal_spans_in_text(
            text,
            {
                "verdict": VERDICT_AUTO_DELETE,
                "span_before": "だからそれは大原あ。",
                "garble_fragment": "だからそれは大原あ。",
                "anomaly_word": "大原あ",
                "evidence": "いや、違うんすよ。だからそれは大原あ。大川社長から",
            },
        )
        self.assertEqual(p["span_before"], "大原あ。")
        self.assertEqual(p["garble_fragment"], "大原あ。")
        self.assertIn("leading_connector_stripped", p["span_alignment"]["actions"])
        start = p["span_start"]
        self.assertEqual(text[:start] + text[start + len(p["span_before"]) :], "いや、違うんすよ。だからそれは大川社長からグループ会社レポート出してくれるってね。")

    def test_plain_filler_anomaly_not_treated_as_connector(self) -> None:
        """Bare fillers (えっと/あの) must still be deleted whole, not preserved."""
        text = "なんですけど、えっと。まずですね。"
        p = align_proposal_spans_in_text(
            text,
            {
                "verdict": VERDICT_AUTO_DELETE,
                "span_before": "えっと",
                "garble_fragment": "えっと",
                "anomaly_word": "えっと",
                "evidence": "なんですけど、えっと。まずですね",
            },
        )
        self.assertEqual(p["span_before"], "えっと")
        self.assertEqual(p["garble_fragment"], "えっと")
        self.assertNotIn("leading_connector_stripped", p["span_alignment"]["actions"])


class SemanticContextWindowTests(unittest.TestCase):
    def test_delete_window_shrinks(self) -> None:
        text = "とか、あとそもそも工程が多い。外いくつ。はい、NEST。"
        span = "そもそも工程が多い。外いくつ"
        start = text.find(span)
        end = start + len(span)
        trial = text[:start] + text[end:]
        before, after = extract_edit_context_pair(text, trial, start, end, window_chars=30)
        self.assertIn("工程", before)
        self.assertNotIn("工程", after)


class SemanticStructuralTests(unittest.TestCase):
    def test_structural_catches_span_wider_than_garble(self) -> None:
        result = structural_delete_overreach(
            {
                "verdict": VERDICT_AUTO_DELETE,
                "proposal_id": "x",
                "span_before": "ロジシンの業務と、合っていますロジシンの業務",
                "garble_fragment": "、合っています",
            }
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.ok)
        self.assertEqual(result.source, "structural")

    def test_removed_beyond_garble(self) -> None:
        beyond = removed_beyond_garble(
            "ロジシンの業務と、合っていますロジシンの業務",
            "、合っています",
        )
        self.assertIn("ロジシン", beyond)

    def test_sync_shrinks_span(self) -> None:
        text = "まず、あのロジシンの業務と、合っていますロジシンの業務の中の課題。"
        span = "ロジシンの業務と、合っていますロジシンの業務"
        start = text.find(span)
        p = {
            "verdict": VERDICT_AUTO_DELETE,
            "span_before": span,
            "span_start": start,
            "garble_fragment": "、合っています",
        }
        self.assertTrue(sync_delete_span_to_garble_fragment(p, text))
        self.assertEqual(p["span_before"], "、合っています")
        self.assertEqual(text[p["span_start"] : p["span_start"] + len(p["span_before"])], "、合っています")


class EmptyAutoCorrectRemediationTests(unittest.TestCase):
    def test_repair_from_reason_pair(self) -> None:
        pair = extract_correction_pair_from_reason(
            "「構造勤務」は「工場勤務」の同音誤り",
            span_before="全員構造勤務みたいな",
        )
        self.assertEqual(pair, ("構造勤務", "工場勤務"))
        repaired = repair_span_after_from_correction("全員構造勤務みたいな", *pair)
        self.assertEqual(repaired, "全員工場勤務みたいな")

    def test_repair_fuzzy_whitespace(self) -> None:
        pair = extract_correction_pair_from_reason(
            "「CHRをクラス」は「CHRクラス」の崩れ",
            span_before="社長とかね CHR をクラスを引っ張り出してきて",
        )
        self.assertIsNotNone(pair)
        assert pair is not None
        repaired = repair_span_after_from_correction(
            "社長とかね CHR をクラスを引っ張り出してきて", *pair
        )
        self.assertIn("CHRクラス", repaired or "")

    def test_pattern_b_downgrades_to_ask_not_delete(self) -> None:
        p = remediate_empty_auto_correct(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "span_before": "各切り口で儲かりのフェアをやりまなぞ。なぞ対策を議論していく",
                "span_after": "",
                "fact_class": "lexical_fluency",
                "reason": "「儲かりのフェア」「なぞ対策」は崩れ",
            }
        )
        self.assertNotEqual(p["verdict"], VERDICT_AUTO_DELETE)
        self.assertIn(
            p["verdict"],
            (VERDICT_ASK_WITH_CANDIDATE, VERDICT_ASK_WITHOUT_CANDIDATE),
        )

    def test_clear_garble_downgrades_to_delete(self) -> None:
        p = remediate_empty_auto_correct(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "span_before": "これ、こうかだからな",
                "span_after": "",
                "fact_class": "filler_garble",
                "reason": "言い直し残骸",
            }
        )
        self.assertEqual(p["verdict"], VERDICT_AUTO_DELETE)
        self.assertEqual(p.get("empty_auto_correct_remediation"), "downgrade_to_auto_delete")

    def test_repaired_keeps_auto_correct(self) -> None:
        p = remediate_empty_auto_correct(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "span_before": "これを元に送信昇格やりたいとか",
                "span_after": "",
                "fact_class": "lexical_fluency",
                "reason": "「送信昇格」は「昇進昇格」の同音誤り",
            }
        )
        self.assertEqual(p["verdict"], VERDICT_AUTO_CORRECT)
        self.assertTrue(str(p.get("span_after") or "").strip())
        self.assertEqual(p.get("empty_auto_correct_remediation"), "span_after_repaired")


class LexicalAmbiguityGuardTests(unittest.TestCase):
    def test_eiga_to_eshi_downgrades_to_ask_without_candidate(self) -> None:
        p = enforce_ambiguous_lexical_auto_correct(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "fact_class": "lexical_fluency",
                "span_before": "想定がまず 1 個の映画ですね",
                "span_after": "想定がまず 1 個の絵姿ですね",
                "reason": "「映画」は「絵姿」の誤変換",
                "hypothesis": "",
                "span_start": 100,
                "span_end": 115,
            },
            text="想定がまず 1 個の映画ですね。だまそのなんだろうな。",
            peer_proposals=[
                {
                    "verdict": VERDICT_ASK_WITH_CANDIDATE,
                    "span_start": 130,
                }
            ],
        )
        self.assertEqual(p["verdict"], VERDICT_ASK_WITHOUT_CANDIDATE)
        self.assertEqual(p.get("lexical_auto_correct_remediation"), "downgrade_non_homophone_content_word")

    def test_seiyaku_homophone_keeps_auto_correct(self) -> None:
        p = enforce_ambiguous_lexical_auto_correct(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "fact_class": "lexical_fluency",
                "span_before": "だけど全く制約してないうんだ",
                "span_after": "だけど全く成約してないうんだ",
                "reason": "営業文脈で「制約」は「成約」の同音誤り",
                "span_start": 10,
                "span_end": 25,
            },
            text="だけど全く制約してないうんだ。ニーズはあるけど。",
        )
        self.assertEqual(p["verdict"], VERDICT_AUTO_CORRECT)

    def test_kouzou_koujou_homophone_claim_keeps_auto_correct(self) -> None:
        p = enforce_ambiguous_lexical_auto_correct(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "fact_class": "lexical_fluency",
                "span_before": "全員構造勤務みたいな",
                "span_after": "全员工場勤務みたいな",
                "reason": "製造系文脈で「構造」は「工場」の同音誤り",
                "span_start": 5,
                "span_end": 20,
            },
            text="でも本当になんか全員構造勤務みたいな。もうパソコン",
        )
        self.assertEqual(p["verdict"], VERDICT_AUTO_CORRECT)

    def test_yochou_repetition_downgrades(self) -> None:
        text = (
            "みたいないや、うちの会社そうなんすよ。とかね。"
            "いや、やっぱり 40 代へこんでるわ。予兆会社そうなんすよ。"
        )
        p = enforce_ambiguous_lexical_auto_correct(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "fact_class": "lexical_fluency",
                "span_before": "予兆会社そうなんすよ",
                "span_after": "うちの会社そうなんすよ",
                "reason": "「予兆会社」は「うちの会社」の誤変換",
                "span_start": text.find("予兆"),
                "span_end": text.find("予兆") + len("予兆会社そうなんすよ"),
            },
            text=text,
        )
        self.assertEqual(p["verdict"], VERDICT_ASK_WITHOUT_CANDIDATE)
        self.assertEqual(p.get("lexical_auto_correct_remediation"), "downgrade_repetition_inference")

    def test_tekigiki_homophone_keeps_auto_correct(self) -> None:
        p = enforce_ambiguous_lexical_auto_correct(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "fact_class": "lexical_fluency",
                "span_before": "敵聞いてください",
                "span_after": "適宜聞いてください",
                "reason": "「敵」は「適宜」の崩れ",
                "span_start": 50,
                "span_end": 58,
            },
            text="資料を敵聞いてください。次に。",
            peer_proposals=[{"verdict": VERDICT_ASK_WITH_CANDIDATE, "span_start": 55}],
        )
        self.assertEqual(p["verdict"], VERDICT_AUTO_CORRECT)


class EditorDeletePunctuationNormalizeTests(unittest.TestCase):
    def test_ten_maru_collapses_to_maru(self) -> None:
        self.assertEqual(
            normalize_editor_delete_punctuation("なんですけど、。まずですね。"),
            "なんですけど。まずですね。",
        )

    def test_maru_ten_collapses_to_ten(self) -> None:
        self.assertEqual(
            normalize_editor_delete_punctuation("こっちはだから。、秋本さん"),
            "こっちはだから、秋本さん",
        )

    def test_ten_ten_collapses_to_single_ten(self) -> None:
        self.assertEqual(
            normalize_editor_delete_punctuation("だから、、なんか"),
            "だから、なんか",
        )

    def test_maru_maru_collapses_to_single_maru(self) -> None:
        self.assertEqual(
            normalize_editor_delete_punctuation("終わり。。次へ"),
            "終わり。次へ",
        )

    def test_orphan_leading_comma_stripped_per_line(self) -> None:
        self.assertEqual(
            normalize_editor_delete_punctuation("あれか本当に\n、全部増やすという\n、矢崎の中の"),
            "あれか本当に\n全部増やすという\n矢崎の中の",
        )

    def test_untouched_when_no_noise(self) -> None:
        text = "今日の進め方ですけれども、あの仕事力サーベイチームとして。"
        self.assertEqual(normalize_editor_delete_punctuation(text), text)


if __name__ == "__main__":
    unittest.main()
