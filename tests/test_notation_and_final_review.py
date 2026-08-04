"""表記ゆれ照合スキャナーと最終批評パスの回帰テスト（2026-07-09 NREPT案件）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notation_consistency import (
    build_notation_block_for_text,
    format_notation_block,
    scan_notation_inconsistencies,
)


def _surfaces(groups: list[dict]) -> set[str]:
    return {v["surface"] for g in groups for v in g["variants"]}


class NotationScannerTests(unittest.TestCase):
    def test_detects_nenji_nenji_mix(self) -> None:
        text = (
            "グループとして7年次を対象とした研修をやります。"
            "当社の場合って8年時を対象に問題解決やってるんですね。"
            "来年は7年次研修を受けた方が8年時になります。"
        )
        groups = scan_notation_inconsistencies(text)
        surfaces = _surfaces(groups)
        self.assertIn("#年次", surfaces)
        self.assertIn("#年時", surfaces)

    def test_detects_shanai_shanai_mix(self) -> None:
        text = "社内で共有します。社内の状況です。車内でもそこで伝えます。"
        groups = scan_notation_inconsistencies(text)
        surfaces = _surfaces(groups)
        self.assertIn("社内", surfaces)
        self.assertIn("車内", surfaces)

    def test_majority_variant_listed_first(self) -> None:
        text = "社内で共有。社内の話。社内の件。車内でも伝える。"
        groups = scan_notation_inconsistencies(text)
        target = next(g for g in groups if "社内" in {v["surface"] for v in g["variants"]})
        self.assertEqual(target["variants"][0]["surface"], "社内")
        self.assertEqual(target["variants"][0]["count"], 3)

    def test_consistent_text_yields_no_groups(self) -> None:
        text = "7年次の研修を実施します。8年次も同様に7年次と揃えます。社内で共有します。"
        self.assertEqual(scan_notation_inconsistencies(text), [])

    def test_digit_widths_are_normalized(self) -> None:
        # 半角7 と全角８ は同一表層 #年次 に畳まれ、ゆれ扱いしない
        text = "7年次の研修です。８年次も対象です。"
        self.assertEqual(scan_notation_inconsistencies(text), [])

    def test_format_block_contains_instruction_and_pairs(self) -> None:
        text = "社内で共有します。社内の状況。車内でもそこで。"
        block = format_notation_block(scan_notation_inconsistencies(text))
        self.assertIn("表記ゆれ候補", block)
        self.assertIn("社内", block)
        self.assertIn("車内", block)
        self.assertIn("文脈で正誤を判定", block)

    def test_empty_text_gives_empty_block(self) -> None:
        self.assertEqual(build_notation_block_for_text(""), "")


class PersonNameVariantScannerTests(unittest.TestCase):
    """山口/川口 型（非同音の姓の聞き取り揺れ）の回帰テスト（2026-07 THR案件）。"""

    def test_detects_yamaguchi_kawaguchi_mix(self) -> None:
        from notation_consistency import scan_person_name_variants

        text = (
            "お名前お伺いしてもいいですか？どなたが今いらっしゃったんですか？"
            "山口という——川口さんか、山口さん、僕は面識はないかな。"
        )
        groups = scan_person_name_variants(text)
        surfaces = _surfaces(groups)
        self.assertIn("山口", surfaces)
        self.assertIn("川口", surfaces)

    def test_detects_variants_with_title_suffix(self) -> None:
        from notation_consistency import scan_person_name_variants

        text = "山口部長が出向で来られました。川口部長は面識がないですね。"
        groups = scan_person_name_variants(text)
        surfaces = _surfaces(groups)
        self.assertIn("山口", surfaces)
        self.assertIn("川口", surfaces)

    def test_single_name_yields_no_groups(self) -> None:
        from notation_consistency import scan_person_name_variants

        text = "山口さんが担当です。山口部長にも共有します。加藤様も同席です。"
        self.assertEqual(scan_person_name_variants(text), [])

    def test_unrelated_names_are_not_grouped(self) -> None:
        from notation_consistency import scan_person_name_variants

        # 2字違い（相原/加藤）は揺れ候補にしない
        text = "相原さんと加藤さんが参加します。"
        self.assertEqual(scan_person_name_variants(text), [])

    def test_block_contains_names_and_instruction(self) -> None:
        from notation_consistency import (
            format_person_name_block,
            scan_person_name_variants,
        )

        text = "山口という方が来られて。川口さんかもしれないです。"
        block = format_person_name_block(scan_person_name_variants(text))
        self.assertIn("人名ゆれ候補", block)
        self.assertIn("山口", block)
        self.assertIn("川口", block)
        self.assertIn("自動修正せず", block)

    def test_combined_block_builder_includes_person_names(self) -> None:
        text = (
            "山口という方が今期出向で来られました。川口さんか、面識はないですが。"
            "7年次の研修です。8年時も対象です。"
        )
        block = build_notation_block_for_text(text)
        self.assertIn("人名ゆれ候補", block)
        self.assertIn("山口", block)
        self.assertIn("表記ゆれ候補", block)


class MinutesPromptNameGuardTests(unittest.TestCase):
    def test_minutes_prompt_forbids_parenthetical_name_invention(self) -> None:
        from generate_minutes_other_sections import _build_minutes_system_prompt

        prompt = _build_minutes_system_prompt()
        self.assertIn("人名・固有名詞の扱い", prompt)
        self.assertIn("括弧併記", prompt)
        self.assertIn("[要確認]", prompt)

    def test_coherence_prompt_mentions_adjacent_name_variants(self) -> None:
        from coherence_review import _build_system_prompt

        prompt = _build_system_prompt(None)
        text = (
            "".join(
                str(b.get("text") or "") if isinstance(b, dict) else str(b)
                for b in prompt
            )
            if isinstance(prompt, list)
            else str(prompt)
        )
        self.assertIn("聞き取り揺れ", text)
        self.assertIn("川口さんか", text)


class PromptInjectionTests(unittest.TestCase):
    @staticmethod
    def _flatten(prompt) -> str:
        if isinstance(prompt, list):
            return "".join(
                str(b.get("text") or "") if isinstance(b, dict) else str(b)
                for b in prompt
            )
        return str(prompt)

    def test_coherence_prompt_includes_notation_block(self) -> None:
        from coherence_review import _build_system_prompt

        block = "\n\n【表記ゆれ候補（機械抽出・全文照合済み）】\n- 『#年次』x2 ⇔ 『#年時』x1"
        text = self._flatten(_build_system_prompt(None, notation_block=block))
        self.assertIn("表記ゆれ候補", text)
        self.assertIn("#年時", text)

    def test_editor_prompt_includes_notation_block(self) -> None:
        from contextual_editor import _build_system_prompt

        block = "\n\n【表記ゆれ候補（機械抽出・全文照合済み）】\n- 『社内』x4 ⇔ 『車内』x1"
        text = self._flatten(_build_system_prompt(None, notation_block=block))
        self.assertIn("表記ゆれ候補", text)
        self.assertIn("車内", text)

    def test_final_review_prompt_requires_full_minutes_cross_check(self) -> None:
        from final_review_pass import _build_system_prompt

        text = self._flatten(_build_system_prompt(""))
        self.assertIn("要約セクションと発言録", text)
        self.assertIn("決定事項・残論点・Next Action と発言録を相互に照合", text)
        self.assertIn("機械抽出された人名ゆれ候補は必ず前後の組織・役割まで照合", text)
        self.assertIn("山口という——川口さんか", text)
        self.assertIn("会議プロファイルの参加者名は", text)
        self.assertIn("両群にそれぞれ", text)
        self.assertIn("組織・役割が異なるなら表記ゆれとして報告しない", text)
        self.assertIn("例: PLS", text)
        self.assertIn("崩れ断片・誤認識", text)
        self.assertIn("問題なし", text)

    def test_final_review_defaults_to_opus(self) -> None:
        from anthropic_prompt_cache import OPUS_MODEL_ID
        from final_review_pass import resolve_final_review_model

        with patch.dict(os.environ, {"FINAL_REVIEW_MODEL": ""}):
            self.assertEqual(resolve_final_review_model(), OPUS_MODEL_ID)


class FinalReviewApplyTests(unittest.TestCase):
    def test_high_confidence_unique_quote_is_applied(self) -> None:
        from final_review_pass import apply_safe_fixes

        text = "当社として8年時を対象とした問題解決研修をやります。"
        findings = [
            {
                "type": "notation",
                "quote": "8年時を対象とした",
                "issue": "7年次と表記不統一",
                "fix": "8年次を対象とした",
                "confidence": "high",
            }
        ]
        out, applied, skipped = apply_safe_fixes(text, findings)
        self.assertIn("8年次を対象とした", out)
        self.assertNotIn("8年時", out)
        self.assertEqual(len(applied), 1)
        self.assertEqual(skipped, [])

    def test_medium_confidence_is_not_applied(self) -> None:
        from final_review_pass import apply_safe_fixes

        text = "そういう示唆で仕事をしなければいけない。"
        findings = [
            {
                "type": "unnatural",
                "quote": "そういう示唆で仕事を",
                "issue": "指示の誤変換の疑い",
                "fix": "そういう指示で仕事を",
                "confidence": "medium",
            }
        ]
        out, applied, skipped = apply_safe_fixes(text, findings)
        self.assertEqual(out, text)
        self.assertEqual(applied, [])
        self.assertEqual(skipped[0]["skip_reason"], "not_high_or_no_fix")

    def test_ambiguous_quote_is_skipped(self) -> None:
        from final_review_pass import apply_safe_fixes

        text = "はいで、A。はいで、B。"
        findings = [
            {
                "type": "backchannel",
                "quote": "はいで、",
                "issue": "相槌の織り込み",
                "fix": "で、",
                "confidence": "high",
            }
        ]
        out, applied, skipped = apply_safe_fixes(text, findings)
        self.assertEqual(out, text)
        self.assertEqual(applied, [])
        self.assertIn("quote_count=2", skipped[0]["skip_reason"])

    def test_flagged_span_is_never_touched(self) -> None:
        from final_review_pass import apply_safe_fixes

        text = "グループの7年次[要確認]研修をやります。"
        findings = [
            {
                "type": "notation",
                "quote": "7年次[要確認]研修",
                "issue": "x",
                "fix": "7年次研修",
                "confidence": "high",
            }
        ]
        out, applied, skipped = apply_safe_fixes(text, findings)
        self.assertEqual(out, text)
        self.assertEqual(skipped[0]["skip_reason"], "flagged_span")

    def test_rewrite_with_new_words_is_skipped(self) -> None:
        from final_review_pass import apply_safe_fixes

        text = "来年度のL2問題、うん、実施をしない方向、はいに考えています。"
        findings = [
            {
                "type": "backchannel",
                "quote": "来年度のL2問題、うん、実施をしない方向、はいに考えています",
                "issue": "相槌の織り込み",
                # 語の推測追加（解決研修/で）を伴う書き換え → 適用しない
                "fix": "来年度のL2問題解決研修は実施をしない方向で考えています",
                "confidence": "high",
            }
        ]
        out, applied, skipped = apply_safe_fixes(text, findings)
        self.assertEqual(out, text)
        self.assertEqual(applied, [])
        self.assertIn("too_many_new_chars", skipped[0]["skip_reason"])

    def test_pure_deletion_backchannel_fix_is_applied(self) -> None:
        from final_review_pass import apply_safe_fixes

        text = "A1が4クラス80名、はいで、L2が5クラスで109名です。"
        findings = [
            {
                "type": "backchannel",
                "quote": "A1が4クラス80名、はいで、L2が5クラス",
                "issue": "相槌の織り込み",
                "fix": "A1が4クラス80名、L2が5クラス",
                "confidence": "high",
            }
        ]
        out, applied, skipped = apply_safe_fixes(text, findings)
        self.assertEqual(out, "A1が4クラス80名、L2が5クラスで109名です。")
        self.assertEqual(len(applied), 1)

    def test_mode_resolution(self) -> None:
        from final_review_pass import resolve_final_review_mode

        with patch.dict(os.environ, {"FINAL_REVIEW_MODE": ""}):
            self.assertEqual(resolve_final_review_mode(), "shadow")
        with patch.dict(os.environ, {"FINAL_REVIEW_MODE": "apply"}):
            self.assertEqual(resolve_final_review_mode(), "apply")
        with patch.dict(os.environ, {"FINAL_REVIEW_MODE": "off"}):
            self.assertEqual(resolve_final_review_mode(), "off")
        with patch.dict(os.environ, {"FINAL_REVIEW_MODE": "bogus"}):
            self.assertEqual(resolve_final_review_mode(), "shadow")

    def test_off_mode_makes_no_api_call(self) -> None:
        from final_review_pass import run_final_review

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"FINAL_REVIEW_MODE": "off"}):
                out, report = run_final_review(job_dir=tmp, text="本文です。")
            self.assertEqual(out, "本文です。")
            self.assertEqual(report["mode"], "off")
            self.assertFalse(
                os.path.isfile(os.path.join(tmp, "final_review_report.json"))
            )

    def test_apply_mode_rechecks_after_applied_fix(self) -> None:
        from final_review_pass import run_final_review

        first = [
            {
                "type": "notation",
                "quote": "8年時を対象とした",
                "issue": "表記不統一",
                "fix": "8年次を対象とした",
                "confidence": "high",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"FINAL_REVIEW_MODE": "apply"}):
                with patch(
                    "final_review_pass._call_reviewer",
                    side_effect=[first, []],
                ) as reviewer:
                    out, report = run_final_review(
                        job_dir=tmp,
                        text="8年時を対象とした研修です。",
                    )
        self.assertIn("8年次", out)
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(report["findings"], [])
        self.assertEqual(len(report["rounds"]), 2)


class ReadablePromptBackchannelRuleTests(unittest.TestCase):
    def test_prompt_contains_backchannel_separation_rule(self) -> None:
        from readable_transcript import _build_system_prompt

        prompt = _build_system_prompt(None)
        text = (
            "".join(
                str(b.get("text") or "") if isinstance(b, dict) else str(b)
                for b in prompt
            )
            if isinstance(prompt, list)
            else str(prompt)
        )
        self.assertIn("相槌の織り込み解消", text)
        self.assertIn("はいで、L2が5クラスで109名", text)


if __name__ == "__main__":
    unittest.main()
