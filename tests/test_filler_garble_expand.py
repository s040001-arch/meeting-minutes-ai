"""Tests for supplemental ④ filler_garble expansion."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from contextual_editor import refresh_proposal_routing
from edit_proposal_schema import VERDICT_AUTO_DELETE, align_proposal_spans_in_text, normalize_verdict
from filler_garble_expand import expand_filler_garble_proposals
from meeting_profile import load_meeting_profile

ROOT = Path(__file__).resolve().parents[1]


class FillerExpandUnitTests(unittest.TestCase):
    def test_mochiron_san_untouched(self) -> None:
        text = "えっと駆動車。もちろんさんもあの時間があればべきなんですけど"
        props = expand_filler_garble_proposals(text, [])
        pos = text.find("もちろんさん")
        for p in props:
            start = int(p.get("span_start") or -1)
            span = str(p.get("span_before") or "")
            if start <= pos < start + len(span):
                self.fail(f"delete overlaps もちろんさん: {p}")

    def test_mochiron_stutter_still_deleted(self) -> None:
        text = "勉強会的なのって出ていただいたんでした。あ、もちろんもちろん、じゃあちょっと"
        props = expand_filler_garble_proposals(text, [])
        self.assertTrue(any(p.get("span_before") == "もちろん" for p in props))
        text = "ありがとうございます。いきなりいきなり仕切り出してくれて。"
        props = expand_filler_garble_proposals(text, [])
        spans = [p["span_before"] for p in props]
        self.assertIn("いきなり", spans)

    def test_greeting_duplicate(self) -> None:
        text = "お疲れ様。。お願いします。。。お願いします。今日の進め方です。"
        props = expand_filler_garble_proposals(text, [])
        joined = " ".join(p["span_before"] for p in props)
        self.assertIn("お願いします", joined)

    def test_filler_etto_between_words(self) -> None:
        text = "それとも何かえっとTHRさんとすでに始まってる部分"
        props = expand_filler_garble_proposals(text, [])
        self.assertTrue(any(p["span_before"] == "えっと" for p in props))

    def test_skips_substantive_numeric(self) -> None:
        text = "50万円50万円で売ってました"
        props = expand_filler_garble_proposals(text, [])
        self.assertEqual(props, [])


class FillerExpand164142Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        jobs = list((ROOT / "data" / "transcriptions").glob("job_20260330_164142*"))
        if not jobs:
            raise unittest.SkipTest("164142 job not local")
        cls.job = jobs[0]
        cls.text = (cls.job / "merged_transcript_mechanical.txt").read_text(encoding="utf-8")
        cls.profile = load_meeting_profile(str(cls.job))
        doc = json.loads((cls.job / "edit_proposals.json").read_text(encoding="utf-8"))
        cls.props = [copy.deepcopy(p) for p in doc.get("proposals") or []]
        for p in cls.props:
            align_proposal_spans_in_text(cls.text, p)

    def test_expand_increases_auto_delete(self) -> None:
        raw_props = [
            copy.deepcopy(p) for p in self.props if not p.get("supplemental_filler_expand")
        ]
        for p in raw_props:
            align_proposal_spans_in_text(self.text, p)
        before = sum(
            1 for p in raw_props if normalize_verdict(p.get("verdict")) == VERDICT_AUTO_DELETE
        )
        refresh_proposal_routing(raw_props, meeting_profile=self.profile, text=self.text)
        after = sum(
            1 for p in raw_props if normalize_verdict(p.get("verdict")) == VERDICT_AUTO_DELETE
        )
        self.assertGreater(after, before)
        self.assertGreaterEqual(after, 8)

    def test_expand_is_idempotent_on_repeated_refresh(self) -> None:
        """Re-running refresh_proposal_routing must not re-add supplemental ④
        proposals — duplicates of tandem-repeated words (いきなりいきなり等)
        cause both occurrences to be deleted instead of one."""
        refresh_proposal_routing(self.props, meeting_profile=self.profile, text=self.text)
        after_first = sum(
            1 for p in self.props if normalize_verdict(p.get("verdict")) == VERDICT_AUTO_DELETE
        )
        refresh_proposal_routing(self.props, meeting_profile=self.profile, text=self.text)
        after_second = sum(
            1 for p in self.props if normalize_verdict(p.get("verdict")) == VERDICT_AUTO_DELETE
        )
        self.assertEqual(after_first, after_second)


if __name__ == "__main__":
    unittest.main()
