import tempfile
from pathlib import Path
from typing import List, Tuple

from openai import OpenAI

from config.settings import settings
from utils.logger import logger

_WHISPER_MAX_BYTES: int = 24 * 1024 * 1024  # 24 MB (OpenAI limit is 25 MB)
_WHISPER_MAX_CHUNK_SECONDS: float = 1390.0
_WHISPER_DURATION_LIMIT_SECONDS: float = 1400.0

client = OpenAI(api_key=settings.OPENAI_API_KEY)

_TRANSCRIPTION_SKIPPED_NO_QUOTA = "[TRANSCRIPTION_SKIPPED_NO_QUOTA]"
_TRANSCRIPTION_SKIPPED_FFMPEG_NOT_FOUND = "[TRANSCRIPTION_SKIPPED_FFMPEG_NOT_FOUND]"


def _extract_chunk_index(file_path: str) -> int:
    path = Path(file_path)
    stem = path.stem

    if "_chunk_" in stem:
        try:
            return int(stem.rsplit("_chunk_", 1)[1])
        except ValueError:
            pass

    return 0


def _transcribe_single_chunk(file_path: str) -> Tuple[int, str]:
    chunk_index = _extract_chunk_index(file_path)

    logger.info(f"Starting transcription: {file_path}")

    with open(file_path, "rb") as audio_file:
        try:
            response = client.audio.transcriptions.create(
                model=settings.WHISPER_MODEL,
                file=audio_file,
                language=settings.WHISPER_LANGUAGE,
            )
        except Exception as exc:
            error_text = str(exc)
            if "insufficient_quota" in error_text or "429" in error_text:
                logger.warning(
                    "Whisper transcription skipped due to quota limit (429/insufficient_quota): %s",
                    file_path,
                )
                return chunk_index, _TRANSCRIPTION_SKIPPED_NO_QUOTA
            if "413" in error_text or "content size limit" in error_text.lower():
                logger.warning(
                    "File too large (413). Falling back to split transcription: %s", file_path
                )
                return chunk_index, _split_and_transcribe(file_path)
            raise

    text = response.text.strip()

    logger.info(f"Transcription completed: {file_path}")

    return chunk_index, text


def _get_duration_seconds(file_path: str) -> float:
    import subprocess
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def _split_and_transcribe(file_path: str) -> str:
    import subprocess

    logger.info("Splitting large audio file via ffmpeg: %s", file_path)

    total_bytes = Path(file_path).stat().st_size
    try:
        duration_sec = _get_duration_seconds(file_path)
    except FileNotFoundError:
        logger.warning(
            "ffprobe not found. Cannot inspect large audio file for split transcription: %s",
            file_path,
        )
        return _TRANSCRIPTION_SKIPPED_FFMPEG_NOT_FOUND
    except Exception as exc:
        logger.warning("ffprobe failed (%s). Estimating duration from file size.", exc)
        duration_sec = total_bytes / 16000  # rough fallback: ~16 KB/s for m4a

    chunk_sec = min(_WHISPER_MAX_CHUNK_SECONDS, duration_sec)

    chunks_text: List[str] = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        chunk_index = 0
        offset = 0.0
        while offset < duration_sec:
            chunk_path = str(Path(tmp_dir) / f"split_chunk_{chunk_index}.wav")
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-ss", str(offset),
                        "-t", str(chunk_sec),
                        "-i", file_path,
                        "-acodec", "pcm_s16le",
                        "-vn",
                        chunk_path,
                    ],
                    capture_output=True,
                    check=True,
                )
            except FileNotFoundError:
                logger.warning(
                    "ffmpeg not found. Cannot split large audio file. Install ffmpeg and ensure ffmpeg is on PATH: %s",
                    file_path,
                )
                return _TRANSCRIPTION_SKIPPED_FFMPEG_NOT_FOUND
            logger.info("Transcribing split chunk %d: %s", chunk_index, chunk_path)
            chunk_obj = Path(chunk_path)
            chunk_exists = chunk_obj.exists()
            chunk_size = chunk_obj.stat().st_size if chunk_exists else -1
            logger.info(
                "WHISPER_PRE_SEND split_chunk path=%s exists=%s size_bytes=%s extension=%s",
                chunk_path,
                chunk_exists,
                chunk_size,
                chunk_obj.suffix,
            )
            with open(chunk_path, "rb") as f:
                try:
                    response = client.audio.transcriptions.create(
                        model=settings.WHISPER_MODEL,
                        file=f,
                        language=settings.WHISPER_LANGUAGE,
                    )
                except Exception as exc:
                    error_text = str(exc)
                    if "insufficient_quota" in error_text or "429" in error_text:
                        logger.warning(
                            "Whisper split transcription skipped due to quota limit (429/insufficient_quota): %s",
                            chunk_path,
                        )
                        return _TRANSCRIPTION_SKIPPED_NO_QUOTA
                    raise
            chunks_text.append(response.text.strip())
            offset += chunk_sec
            chunk_index += 1

    merged = "\n".join(t for t in chunks_text if t)
    logger.info("Split transcription completed. chunks=%d", chunk_index)
    return merged


def transcribe_audio(file_paths: List[str] | str) -> str:
    if isinstance(file_paths, str):
        try:
            duration_sec = _get_duration_seconds(file_paths)
            if duration_sec >= _WHISPER_DURATION_LIMIT_SECONDS:
                logger.info(
                    "Audio duration %.2f sec exceeds limit. Using split transcription: %s",
                    duration_sec,
                    file_paths,
                )
                return _split_and_transcribe(file_paths)
        except Exception:
            pass

        if Path(file_paths).stat().st_size > _WHISPER_MAX_BYTES:
            logger.info(
                "File exceeds %d bytes. Using split transcription: %s",
                _WHISPER_MAX_BYTES,
                file_paths,
            )
            return _split_and_transcribe(file_paths)
        logger.info(
            "Audio is within direct transcription limits. ffmpeg is not required: %s",
            file_paths,
        )
        _, text = _transcribe_single_chunk(file_paths)
        return text

    transcribed_chunks = [_transcribe_single_chunk(file_path) for file_path in file_paths]
    transcribed_chunks.sort(key=lambda item: item[0])

    merged_text = "\n".join(text for _, text in transcribed_chunks if text).strip()

    logger.info("All chunk transcriptions merged in sorted order")

    return merged_text


def transcribe_with_whisper(file_paths: List[str] | str) -> str:
    return transcribe_audio(file_paths)
