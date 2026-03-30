import argparse
import json
import os
from datetime import datetime, timezone
from faster_whisper import WhisperModel


def main() -> None:
    parser = argparse.ArgumentParser(
        description="1チャンク音声をfaster-whisperで文字起こし（Task 3-1）"
    )
    parser.add_argument("--input", required=True, help="入力チャンク音声ファイルパス")
    parser.add_argument(
        "--model",
        default="small",
        help="Whisperモデル名（例: tiny, base, small）",
    )
    parser.add_argument(
        "--language",
        default="ja",
        help="言語コード（デフォルト: ja）",
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="計算タイプ（CPU向け既定: int8）",
    )
    parser.add_argument("--job-id", required=True, help="ジョブ識別子（例: job_20260324_001）")
    parser.add_argument(
        "--chunk-id",
        default="chunk_000",
        help="チャンク識別子（デフォルト: chunk_000）",
    )
    parser.add_argument(
        "--chunk-index",
        type=int,
        default=0,
        help="チャンク番号（デフォルト: 0）",
    )
    parser.add_argument(
        "--start-sec",
        type=float,
        default=0.0,
        help="チャンク開始秒（デフォルト: 0.0）",
    )
    parser.add_argument(
        "--end-sec",
        type=float,
        default=30.0,
        help="チャンク終了秒（デフォルト: 30.0）",
    )
    parser.add_argument(
        "--output-root",
        default="data/transcriptions",
        help="保存ルートディレクトリ（デフォルト: data/transcriptions）",
    )
    args = parser.parse_args()

    try:
        model = WhisperModel(args.model, device="cpu", compute_type=args.compute_type)
        segments, info = model.transcribe(args.input, language=args.language)
    except Exception as e:
        failed_log = {
            "chunk_id": args.chunk_id,
            "status": "failed",
            "error": str(e),
        }
        print(json.dumps(failed_log, ensure_ascii=False))
        raise

    text_parts = []
    for seg in segments:
        text_parts.append(seg.text.strip())

    transcript = " ".join(p for p in text_parts if p).strip()
    status = "success" if transcript else "failed"
    error = "" if transcript else "empty_transcript"

    # Task 3-2 最小実装:
    # job_id ディレクトリを作成し、1チャンク結果をJSONで保存する。
    job_dir = os.path.join(args.output_root, args.job_id)
    os.makedirs(job_dir, exist_ok=True)
    output_json_path = os.path.join(job_dir, f"{args.chunk_id}.json")
    payload = {
        "job_id": args.job_id,
        "chunk_id": args.chunk_id,
        "chunk_index": args.chunk_index,
        "start_sec": args.start_sec,
        "end_sec": args.end_sec,
        "status": status,
        "text": transcript,
        "error": error,
        "detected_language": info.language,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    if status == "success":
        success_log = {
            "chunk_id": args.chunk_id,
            "status": "success",
            "text": transcript,
            "duration": int(max(0, args.end_sec - args.start_sec)),
        }
        print(json.dumps(success_log, ensure_ascii=False))
    else:
        failed_log = {
            "chunk_id": args.chunk_id,
            "status": "failed",
            "error": error,
        }
        print(json.dumps(failed_log, ensure_ascii=False))

    print(f"input={args.input}")
    print(f"model={args.model}")
    print(f"language={args.language}")
    print(f"detected_language={info.language}")
    print(f"job_id={args.job_id}")
    print(f"chunk_id={args.chunk_id}")
    print(f"saved_json={output_json_path}")
    print("text=")
    print(transcript)


if __name__ == "__main__":
    main()

