import json
from pathlib import Path

src = Path("data/whisper_split_checkpoint_test.json")
payload = json.loads(src.read_text(encoding="utf-8"))

# Keep existing chunks 0-3 (real text)
existing_chunks = {item["index"]: item["text"] for item in (payload.get("chunks") or [])}

# Add placeholder text for chunks 4-9
for i in range(4, 10):
    if i not in existing_chunks:
        existing_chunks[i] = f"（チャンク{i}のテスト用プレースホルダーテキスト。実際の文字起こしは省略しています。）"

# fast-forward to chunk 10
payload["next_chunk_index"] = 10
payload["chunks"] = [{"index": k, "text": v} for k, v in sorted(existing_chunks.items())]

dst = Path("data/whisper_split_checkpoint_finaltest.json")
dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"CREATED: {dst}")
print(f"next_chunk_index: {payload['next_chunk_index']}")
print(f"cached_chunks: {len(payload['chunks'])}")
print(f"source_fingerprint: {payload['source_fingerprint'][:60]}...")
