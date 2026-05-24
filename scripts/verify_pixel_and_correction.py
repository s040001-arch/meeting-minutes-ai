#!/usr/bin/env python3
"""Local verification: Pixel fixes + optional full Opus correction dry-run."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from ai_correct_text import correct_full_text, get_last_correct_full_text_meta
from mechanical_correct_text import apply_pixel_recognizer_fixes

PIXEL_BAD = (
    "最高用",
    "最高用語",
    "最高業者",
    "天然前",
    "天然デジャ",
    "食卓社員",
    "食卓定年最高用",
    "公認育成",
    "リネン戦略",
    "ミンチ政策",
    "インギージメントサーベリー",
)
PIXEL_OK = (
    "再雇用",
    "定年前",
    "嘱託社員",
    "後輩育成",
    "理念戦略",
    "認知施策",
    "エンゲージメントサーベイ",
)


def _count_hits(text: str, needles: tuple[str, ...]) -> dict[str, int]:
    return {n: text.count(n) for n in needles}


def main() -> int:
    mech_path = (
        REPO
        / "data/transcriptions/_railway_fetch"
        / "job_20260524_102914_2026_0521_エレマテック社_シニア研修_理念浸透研修_後藤様_高屋様_臼"
        / "merged_transcript_mechanical.txt"
    )
    if not mech_path.is_file():
        print(f"[skip] mechanical file missing: {mech_path}")
        return 1

    raw = mech_path.read_text(encoding="utf-8")
    fixed = apply_pixel_recognizer_fixes(raw)
    bad_before = sum(_count_hits(raw, PIXEL_BAD).values())
    bad_after = sum(_count_hits(fixed, PIXEL_BAD).values())
    print(f"pixel_dict_only: bad_hits {bad_before} -> {bad_after}")
    if bad_after:
        for n, c in _count_hits(fixed, PIXEL_BAD).items():
            if c:
                print(f"  remaining {n}: {c}")

    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("[skip] ANTHROPIC_API_KEY unset — full Opus dry-run skipped")
        return 0 if bad_after == 0 else 1

    out_dir = REPO / "data/transcriptions/local_verify_pixel_20260524"
    out_dir.mkdir(parents=True, exist_ok=True)
    print("running correct_full_text (Opus)...")
    result = correct_full_text(fixed, visible_log_path=str(out_dir / "visible.log"))
    meta = get_last_correct_full_text_meta()
    (out_dir / "merged_transcript_ai.txt").write_text(result, encoding="utf-8")
    (out_dir / "correction_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ratio = float(meta.get("ratio") or 0)
    bad_final = sum(_count_hits(result, PIXEL_BAD).values())
    yoisho = "よいしょ" in result
    retries = sum(int(c.get("retry_count") or 0) for c in meta.get("chunk_results") or [])
    first_pass_fail = sum(
        1
        for c in meta.get("chunk_results") or []
        if not c.get("tail_covered") and int(c.get("retry_count") or 0) > 0
    )

    print(f"ratio={ratio:.3f} output_chars={meta.get('output_chars')} yoisho={yoisho}")
    print(f"pixel_bad_hits={bad_final} chunk_retries_total={retries}")
    print("chunk_results:")
    for c in meta.get("chunk_results") or []:
        print(
            f"  chunk={c.get('chunk_index')} tail={c.get('tail_covered')} "
            f"retry={c.get('retry_count')} used_original={c.get('used_original')} "
            f"method={c.get('tail_check_method')}"
        )

    ok = (
        ratio >= 0.85
        and yoisho
        and bad_final == 0
        and retries == 0
        and meta.get("full_tail_covered")
    )
    print("VERIFY_OK=" + str(ok))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
