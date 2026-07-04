"""Read back Google Doc for job_20260701_053826 and verify TypeA+TypeB markers.

Run on Railway after reprocess_job.py --from-step 6.1.
Does not rely on full_write_verified alone — fetches live Doc text.
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, "/app")

from export_minutes_to_google_docs import (  # noqa: E402
    load_or_create_google_docs_credentials,
    fetch_google_doc_text_with_retry,
)
from googleapiclient.discovery import build  # type: ignore  # noqa: E402

DOC_ID = "1wsXCbkpW4HRV1GjF-XHK_hM3I9vCBo91VOWCg9B5yDQ"

# (term, min_count, description)
CHECKS: list[tuple[str, int, str]] = [
    ("合瀬", 1, "TypeA: 大瀬→合瀬"),
    ("中本絵麻", 1, "TypeA: 中本エマ→絵麻"),
    ("戦略推進室", 1, "TypeA: 人脈推進室→戦略推進室"),
    ("鷹股", 1, "TypeA: 坂本→鷹股(部長コンテキスト)"),
    ("精度が高", 1, "TypeA: 制度高い→精度が高い"),
    ("参加は任意", 1, "TypeB A-1: 任意参加の再構成"),
    ("ブランドリフトサーベイ", 1, "既反映: ブランドリフトサーベイ"),
    ("定性データ", 1, "既反映: 定性データ"),
]

ABSENT: list[tuple[str, str]] = [
    ("大瀬", "旧誤: 大瀬"),
    ("中本エマ", "旧誤: 中本エマ"),
    ("人脈推進室", "旧誤: 人脈推進室"),
    ("[補足:", "TypeB: [補足:] 注釈は除去済みであること"),
    ("記号を取ってる——いや、でもいいんじゃないかなと", "TypeB A-1: 旧ガーブル残存"),
]


def main() -> int:
    creds = load_or_create_google_docs_credentials("credentials.json", "token.json")
    docs_service = build("docs", "v1", credentials=creds, cache_discovery=False)
    doc_text = fetch_google_doc_text_with_retry(docs_service, DOC_ID)

    print(f"doc_chars={len(doc_text)}")
    print(f"[補足:] count={doc_text.count('[補足:')}")
    print()

    ok = True
    print("=== must-present ===")
    for term, min_count, desc in CHECKS:
        cnt = doc_text.count(term)
        passed = cnt >= min_count
        if not passed:
            ok = False
        mark = "✓" if passed else "✗"
        print(f"  {mark} {desc}: {cnt}件 (min={min_count})")

    print()
    print("=== must-absent ===")
    for term, desc in ABSENT:
        cnt = doc_text.count(term)
        passed = cnt == 0
        if not passed:
            ok = False
        mark = "✓" if passed else "✗"
        print(f"  {mark} {desc}: {cnt}件")

    print()
    print("--- Doc head (600 chars) ---")
    print(doc_text[:600])
    print()
    # A-1 region snippet
    idx = doc_text.find("任意")
    if idx >= 0:
        print("--- A-1 region (±80) ---")
        print(doc_text[max(0, idx - 80) : idx + 80])
        print()

    print(f"overall={'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
