import sys, os
sys.stdout.reconfigure(encoding='utf-8')

# Add app path
sys.path.insert(0, '/app')

from export_minutes_to_google_docs import (
    load_or_create_google_docs_credentials,
    fetch_google_doc_text_with_retry,
)
from googleapiclient.discovery import build  # type: ignore

doc_id = '1wsXCbkpW4HRV1GjF-XHK_hM3I9vCBo91VOWCg9B5yDQ'
creds = load_or_create_google_docs_credentials('credentials.json', 'token.json')
docs_service = build('docs', 'v1', credentials=creds, cache_discovery=False)
doc_text = fetch_google_doc_text_with_retry(docs_service, doc_id)

print(f'doc_chars={len(doc_text)}')
print(f'[補足:] count={doc_text.count("[補足:")}')
print()

# Check TypeA corrections appear in minutes
checks = [
    ('合瀬', '大瀬→合瀬 反映'),
    ('中本絵麻', '中本エマ→絵麻 反映'),
    ('戦略推進室', '人脈推進室→戦略推進室 反映'),
    ('鷹股', '鷹股さん references'),
    ('精度', '精度が高い references'),
]
for term, desc in checks:
    cnt = doc_text.count(term)
    print(f'  {"✓" if cnt > 0 else "·"} {desc}: {cnt}件')

# Show first 500 chars
print()
print('--- Doc head (500 chars) ---')
print(doc_text[:500])
