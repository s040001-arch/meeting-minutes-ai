import argparse
import os
import subprocess
from pathlib import Path


def convert_to_wav(input_path: str, output_path: str, sample_rate: int = 16000) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        output_path,
    ]

    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "ffmpeg conversion failed\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr:\n{completed.stderr}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="音声ファイルをffmpegでwavへ変換（Task 2-1）"
    )
    parser.add_argument("--input", required=True, help="入力音声ファイル（mp3/m4a/wav）")
    parser.add_argument("--output", help="出力wavファイルパス（省略時: 入力と同名で .wav）")
    parser.add_argument("--sample-rate", type=int, default=16000, help="出力サンプリングレート")
    args = parser.parse_args()

    input_path = args.input
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"input file not found: {input_path}")

    output_path = args.output
    if not output_path:
        output_path = str(Path(input_path).with_suffix(".wav"))

    convert_to_wav(input_path, output_path, sample_rate=args.sample_rate)

    print(f"converted_from={input_path}")
    print(f"converted_to={output_path}")
    print(f"sample_rate={args.sample_rate}")


if __name__ == "__main__":
    main()

