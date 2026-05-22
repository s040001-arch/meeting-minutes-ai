import argparse
import json
import os
from datetime import datetime, timezone
from faster_whisper import WhisperModel

# ---------------------------------------------------------------------------
# モデル選択ポリシー（環境変数で上書き可能）
# ---------------------------------------------------------------------------
# WHISPER_MODEL          : 使用するWhisperモデル名（既定: large-v3-turbo）
# WHISPER_COMPUTE_TYPE   : 計算精度（既定: int8）
# WHISPER_VAD_FILTER     : 無音フィルタ有効化（既定: true）
# WHISPER_BEAM_SIZE      : ビーム幅（既定: 5）
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_COMPUTE_TYPE = "int8"


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, "").strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_model_name(cli_model: str | None) -> str:
    if cli_model:
        return cli_model
    env_model = os.getenv("WHISPER_MODEL", "").strip()
    if env_model:
        return env_model
    return DEFAULT_MODEL


def _resolve_compute_type(cli_compute_type: str | None) -> str:
    if cli_compute_type:
        return cli_compute_type
    env_ct = os.getenv("WHISPER_COMPUTE_TYPE", "").strip()
    if env_ct:
        return env_ct
    return DEFAULT_COMPUTE_TYPE


def _load_initial_prompt(job_id: str, output_root: str) -> str:
    """ジョブディレクトリの initial_prompt.txt を読み込む。

    run_job_once.py が音声分割前に作成しておく想定。
    存在しない場合は空文字を返す（faster-whisper では空文字は initial_prompt 無し扱い）。
    """
    path = os.path.join(output_root, job_id, "initial_prompt.txt")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="1チャンク音声をfaster-whisperで文字起こし（Task 3-1）"
    )
    parser.add_argument("--input", required=True, help="入力チャンク音声ファイルパス")
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Whisperモデル名。未指定時は環境変数 WHISPER_MODEL、"
            f"それも無ければ {DEFAULT_MODEL} を使用"
        ),
    )
    parser.add_argument(
        "--language",
        default="ja",
        help="言語コード（デフォルト: ja）",
    )
    parser.add_argument(
        "--compute-type",
        default=None,
        help=(
            "計算タイプ。未指定時は環境変数 WHISPER_COMPUTE_TYPE、"
            f"それも無ければ {DEFAULT_COMPUTE_TYPE}"
        ),
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
    parser.add_argument(
        "--initial-prompt",
        default=None,
        help=(
            "Whisper の initial_prompt として渡す文字列。"
            "未指定時は data/transcriptions/{job_id}/initial_prompt.txt を読む。"
        ),
    )
    args = parser.parse_args()

    use_model = _resolve_model_name(args.model)
    use_compute_type = _resolve_compute_type(args.compute_type)
    use_vad = _env_bool("WHISPER_VAD_FILTER", True)
    try:
        beam_size = int(os.getenv("WHISPER_BEAM_SIZE", "5"))
    except ValueError:
        beam_size = 5

    initial_prompt = (
        args.initial_prompt
        if args.initial_prompt is not None
        else _load_initial_prompt(args.job_id, args.output_root)
    )

    transcribe_kwargs: dict = {
        "language": args.language,
        "vad_filter": use_vad,
        "beam_size": beam_size,
        "condition_on_previous_text": True,
    }
    if initial_prompt:
        transcribe_kwargs["initial_prompt"] = initial_prompt

    # VAD: 短すぎる無音区間のノイズを除去（環境変数 WHISPER_VAD_MIN_SILENCE_MS で調整可）
    if use_vad:
        try:
            min_silence_ms = int(os.getenv("WHISPER_VAD_MIN_SILENCE_MS", "400"))
        except ValueError:
            min_silence_ms = 400
        transcribe_kwargs["vad_parameters"] = {
            "min_silence_duration_ms": max(200, min_silence_ms),
            "speech_pad_ms": 300,
        }

    try:
        model = WhisperModel(use_model, device="cpu", compute_type=use_compute_type)
        segments, info = model.transcribe(args.input, **transcribe_kwargs)
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
        "model": use_model,
        "compute_type": use_compute_type,
        "vad_filter": use_vad,
        "initial_prompt_used": bool(initial_prompt),
        "initial_prompt_chars": len(initial_prompt),
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
    print(f"model={use_model}")
    print(f"compute_type={use_compute_type}")
    print(f"vad_filter={use_vad}")
    print(f"beam_size={beam_size}")
    print(f"initial_prompt_used={bool(initial_prompt)} chars={len(initial_prompt)}")
    print(f"language={args.language}")
    print(f"detected_language={info.language}")
    print(f"job_id={args.job_id}")
    print(f"chunk_id={args.chunk_id}")
    print(f"saved_json={output_json_path}")
    print("text=")
    print(transcript)


if __name__ == "__main__":
    main()
