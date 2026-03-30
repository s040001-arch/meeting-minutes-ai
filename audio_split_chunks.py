import argparse
import os
import subprocess


def split_audio_chunks(
    input_wav: str,
    output_dir: str,
    chunk_seconds: int = 30,
    prefix: str = "chunk",
    max_chunks: int | None = None,
) -> None:
    os.makedirs(output_dir, exist_ok=True)

    output_pattern = os.path.join(output_dir, f"{prefix}_%03d.wav")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_wav,
    ]
    if max_chunks is not None:
        cmd.extend(["-t", str(chunk_seconds * max_chunks)])
    cmd.extend(
        [
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-c",
            "copy",
            output_pattern,
        ]
    )

    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "ffmpeg split failed\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr:\n{completed.stderr}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="wav音声を30秒単位でチャンク分割（Task 2-2）"
    )
    parser.add_argument("--input", required=True, help="入力wavファイルパス")
    parser.add_argument("--output-dir", required=True, help="チャンク出力先ディレクトリ")
    parser.add_argument(
        "--chunk-seconds",
        type=int,
        default=30,
        help="チャンク秒数（デフォルト: 30）",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="生成する最大チャンク数（未指定時は全チャンク）",
    )
    args = parser.parse_args()

    if not os.path.exists(args.input):
        raise FileNotFoundError(f"input file not found: {args.input}")
    if args.max_chunks is not None and args.max_chunks <= 0:
        raise ValueError("--max-chunks must be greater than 0")

    split_audio_chunks(
        input_wav=args.input,
        output_dir=args.output_dir,
        chunk_seconds=args.chunk_seconds,
        max_chunks=args.max_chunks,
    )

    print(f"split_input={args.input}")
    print(f"split_output_dir={args.output_dir}")
    print(f"chunk_seconds={args.chunk_seconds}")
    print(f"max_chunks={args.max_chunks}")


if __name__ == "__main__":
    main()

