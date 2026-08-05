"""learned_corrections_store の scope (global/context) 分離のテスト。"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from learned_corrections_store import (
    add_learned_correction,
    decide_scope,
    format_context_hints_block,
    load_context_hints,
    load_learned_dict,
    suggest_scope,
)


class SuggestScopeTests(unittest.TestCase):
    def test_known_real_words_are_context(self) -> None:
        for w in ["本数", "本社", "給食", "ベッド", "教育機関", "ミスト"]:
            self.assertEqual(suggest_scope(w), "context", w)

    def test_short_words_are_context(self) -> None:
        self.assertEqual(suggest_scope("決済"), "context")
        self.assertEqual(suggest_scope("あや"), "context")

    def test_garble_words_are_global(self) -> None:
        for w in ["就高年収", "自施要項", "講師ング", "オブザード", "倫理決済"]:
            self.assertEqual(suggest_scope(w), "global", w)


class DecideScopeTests(unittest.TestCase):
    """decide_scope: 入口の実在語判定（LLM + 安全側フォールバック）。"""

    def test_name_like_wrongs_are_context_without_llm(self) -> None:
        # 「根本さん」(4文字) は旧ヒューリスティックでは global に漏れていた
        with patch(
            "learned_corrections_store._classify_scope_with_llm"
        ) as llm:
            self.assertEqual(decide_scope("根本さん", "梅本さん"), "context")
            llm.assert_not_called()

    def test_heuristic_context_skips_llm(self) -> None:
        with patch(
            "learned_corrections_store._classify_scope_with_llm"
        ) as llm:
            self.assertEqual(decide_scope("決済", "決裁"), "context")
            llm.assert_not_called()

    def test_llm_verdict_is_used(self) -> None:
        with patch(
            "learned_corrections_store._classify_scope_with_llm",
            return_value="context",
        ):
            self.assertEqual(
                decide_scope("本店にあります", "本当にあります"), "context"
            )
        with patch(
            "learned_corrections_store._classify_scope_with_llm",
            return_value="global",
        ):
            self.assertEqual(
                decide_scope("倫理決済", "稟議決裁"), "global"
            )

    def test_llm_failure_falls_back_to_safe_context(self) -> None:
        # 判定不能時は盲目置換より文脈ヒントが安全
        with patch(
            "learned_corrections_store._classify_scope_with_llm",
            return_value=None,
        ):
            self.assertEqual(
                decide_scope("小さいやつ", "G3/A3研修"), "context"
            )


class ScopeSeparationTests(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)

    def tearDown(self) -> None:
        if os.path.isfile(self.path):
            os.unlink(self.path)

    def test_context_excluded_from_mechanical_dict(self) -> None:
        add_learned_correction(
            wrong="就高年収", right="集合研修", via="chat_fix",
            job_id="j1", scope="global", path=self.path,
        )
        add_learned_correction(
            wrong="本数", right="工数", via="chat_fix",
            job_id="j1", scope="context",
            example="工数がさっき削減って言いましたけど", path=self.path,
        )
        mech = load_learned_dict(self.path)
        self.assertIn("就高年収", mech)
        self.assertNotIn("本数", mech)

        hints = load_context_hints(self.path)
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]["wrong"], "本数")
        self.assertEqual(hints[0]["to"], "工数")
        self.assertIn("削減", hints[0]["example"])

    def test_legacy_entries_without_scope_stay_global(self) -> None:
        # scope なしの既存エントリは従来どおり機械補正対象
        add_learned_correction(
            wrong="倫理決済", right="稟議決裁", via="line_qa",
            job_id="j0", path=self.path,
        )
        mech = load_learned_dict(self.path)
        self.assertIn("倫理決済", mech)
        self.assertEqual(load_context_hints(self.path), [])

    def test_hints_block_format(self) -> None:
        add_learned_correction(
            wrong="聖書", right="弊社", via="chat_fix", job_id="j1",
            scope="context", example="聖書の場合では時間変更", path=self.path,
        )
        block = format_context_hints_block(self.path)
        self.assertIn("『聖書』→『弊社』", block)
        self.assertIn("文脈依存", block)
        # 空ストアなら空文字
        self.assertEqual(format_context_hints_block(self.path + ".none"), "")


if __name__ == "__main__":
    unittest.main()
