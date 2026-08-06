"""correction_sanitizer と speaker_turn_breaks の決定論ゲートのテスト。"""
import unittest
from unittest import mock

from correction_sanitizer import sanitize_correction_text


class SanitizerTests(unittest.TestCase):
    def test_removes_enum_marker_leaked_from_question_template(self):
        # 072038 実害: 「2.」が correct に混入して本文に書き込まれた
        correct = (
            "週報の送信先にプレセナの宛先も入れて次のフィードバックを"
            "してもらうとか 2.こういうフィードバックをして"
        )
        wrong = "週報の送信先にプレセナの宛先も入れて次のフィードバックとい"
        out = sanitize_correction_text(correct, wrong=wrong)
        self.assertNotIn("2.", out)
        self.assertIn("してもらうとかこういうフィードバック", out)

    def test_keeps_marker_when_present_in_wrong(self):
        # 置換元にも同じマーカーがある → 本文由来なので保持
        out = sanitize_correction_text("議題は 2.予算案 です", wrong="議題は 2.予算 です")
        self.assertIn("2.", out)

    def test_decimals_and_dates_are_protected(self):
        out = sanitize_correction_text("半日は70%がけで52.5万です", wrong="x")
        self.assertIn("52.5万", out)
        out2 = sanitize_correction_text("8月12日に持っていきたい", wrong="x")
        self.assertEqual(out2, "8月12日に持っていきたい")

    def test_removes_circled_digits(self):
        out = sanitize_correction_text("①これを直す", wrong="これを直しす")
        self.assertEqual(out, "これを直す")

    def test_empty_and_clean_passthrough(self):
        self.assertEqual(sanitize_correction_text("", wrong="a"), "")
        self.assertEqual(
            sanitize_correction_text("普通の修正文です", wrong="a"),
            "普通の修正文です",
        )


class SpeakerTurnBreaksTests(unittest.TestCase):
    def _run(self, para, llm_output):
        import speaker_turn_breaks as stb

        fake_client = mock.MagicMock()
        with mock.patch.dict(
            "os.environ", {"ANTHROPIC_API_KEY": "x"}
        ), mock.patch.object(stb, "_split_one", return_value=llm_output):
            with mock.patch("anthropic.Anthropic", return_value=fake_client):
                return stb.apply_speaker_turn_breaks(para)

    def test_splits_when_content_identical(self):
        para = "そ" * 60 + "うですね。" + "な" * 60 + "るほど。"
        llm_out = "そ" * 60 + "うですね。\n" + "な" * 60 + "るほど。"
        out, stats = self._run(para, llm_out)
        self.assertEqual(stats["paras_split"], 1)
        self.assertIn("\n\n", out)
        # 改行以外の文字は完全一致
        self.assertEqual(
            out.replace("\n", ""), para.replace("\n", "")
        )

    def test_discards_when_content_changed(self):
        para = "あ" * 120 + "本文です。"
        llm_out = "あ" * 120 + "本文を変えました。"  # 改変された出力
        out, stats = self._run(para, llm_out)
        self.assertEqual(out, para)
        self.assertEqual(stats["paras_split"], 0)
        self.assertEqual(stats["paras_discarded"], 1)

    def test_short_paragraphs_untouched(self):
        para = "短い段落。"
        out, stats = self._run(para, "何か")
        self.assertEqual(out, para)
        self.assertEqual(stats["paras_seen"], 0)

    def test_disabled_by_env(self):
        import speaker_turn_breaks as stb

        with mock.patch.dict(
            "os.environ",
            {"SPEAKER_TURN_BREAKS_ENABLED": "0", "ANTHROPIC_API_KEY": "x"},
        ):
            out, stats = stb.apply_speaker_turn_breaks("あ" * 200)
        self.assertEqual(out, "あ" * 200)
        self.assertFalse(stats["enabled"])


if __name__ == "__main__":
    unittest.main()
