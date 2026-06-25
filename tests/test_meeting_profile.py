"""Tests for meeting_profile participant augmentation (Phase 10 priority-2 fix)."""
from __future__ import annotations

import unittest

from meeting_profile import (
    SYSTEM_OWNER_NAME,
    augment_profile_with_transcript_participants,
)


class OwnerAlwaysIncludedTests(unittest.TestCase):
    def test_owner_added_when_filename_participants_omit_him(self) -> None:
        profile = {"participants": ["高田", "秋本", "季央", "工藤"]}
        merged = augment_profile_with_transcript_participants(profile, "本文")
        self.assertIn(SYSTEM_OWNER_NAME, merged["participants"])
        self.assertEqual(
            merged["participants"], ["高田", "秋本", "季央", "工藤", SYSTEM_OWNER_NAME]
        )

    def test_owner_not_duplicated_when_already_present(self) -> None:
        profile = {"participants": ["高田", SYSTEM_OWNER_NAME]}
        merged = augment_profile_with_transcript_participants(profile, "本文")
        self.assertEqual(merged["participants"].count(SYSTEM_OWNER_NAME), 1)

    def test_owner_present_even_with_no_participants_and_no_transcript_signal(
        self,
    ) -> None:
        merged = augment_profile_with_transcript_participants({}, "特に名前の出てこない本文です。")
        self.assertEqual(merged["participants"], [SYSTEM_OWNER_NAME])

    def test_transcript_inferred_participants_still_merged_with_owner(self) -> None:
        text = "森川さんと話した。森川さんに伝えた。森川さんからの返事。"
        merged = augment_profile_with_transcript_participants({}, text)
        self.assertIn("森川", merged["participants"])
        self.assertIn(SYSTEM_OWNER_NAME, merged["participants"])
        self.assertEqual(merged.get("participants_source"), "transcript_inferred")


if __name__ == "__main__":
    unittest.main()
