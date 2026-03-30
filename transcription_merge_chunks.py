import argparse
import json
import os
from glob import glob
from typing import Any, Dict, List


def _load_chunk_jsons(job_dir: str) -> List[Dict[str, Any]]:
    paths = sorted(glob(os.path.join(job_dir, "chunk_*.json")))
    chunks: List[Dict[str, Any]] = []
    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data["_source_path"] = path
            chunks.append(data)
    return chunks


def _sort_key(chunk: Dict[str, Any]):
    return (
        int(chunk.get("chunk_index", 10**9)),
        float(chunk.get("start_sec", 10**9)),
        str(chunk.get("chunk_id", "")),
    )


def merge_chunk_texts(chunks: List[Dict[str, Any]]) -> str:
    ordered = sorted(chunks, key=_sort_key)
    texts: List[str] = []
    for chunk in ordered:
        if chunk.get("status") != "success":
            continue
        text = str(chunk.get("text", "")).strip()
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="チャンクJSONを時系列順に結合して全文を作成（Task 4-1）"
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="チャンクJSONのルートディレクトリ",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="結合テキストの出力先（未指定時: data/transcriptions/{job_id}/merged_transcript.txt）",
    )
    args = parser.parse_args()

    job_dir = os.path.join(args.input_root, args.job_id)
    if not os.path.isdir(job_dir):
        raise FileNotFoundError(f"job directory not found: {job_dir}")

    chunk_jsons = _load_chunk_jsons(job_dir)
    if not chunk_jsons:
        raise FileNotFoundError(f"chunk json not found in: {job_dir}")

    merged_text = merge_chunk_texts(chunk_jsons)

    output_path = args.output or os.path.join(job_dir, "merged_transcript.txt")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(merged_text)

    print(f"job_id={args.job_id}")
    print(f"chunk_json_count={len(chunk_jsons)}")
    print(f"merged_text_length={len(merged_text)}")
    print(f"saved_to={output_path}")


if __name__ == "__main__":
    main()

