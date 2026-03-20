import tempfile
import time
from pathlib import Path
from typing import List, Tuple

from openai import InternalServerError, OpenAI

from config.settings import settings
from utils.logger import logger

_WHISPER_MAX_BYTES: int = 24 * 1024 * 1024  # 24 MB (OpenAI limit is 25 MB)
_WHISPER_MAX_CHUNK_SECONDS: float = 300.0
_WHISPER_DURATION_LIMIT_SECONDS: float = 1400.0
_WHISPER_SPLIT_SAMPLE_RATE: int = 16000
_WHISPER_SPLIT_CHANNELS: int = 1
_WHISPER_SPLIT_BYTES_PER_SAMPLE: int = 2  # pcm_s16le
_WHISPER_SPLIT_SIZE_SAFETY_MARGIN: float = 0.85
_WHISPER_TRANSCRIBE_MAX_RETRIES: int = 6

client = OpenAI(api_key=settings.OPENAI_API_KEY)

_TRANSCRIPTION_SKIPPED_NO_QUOTA = "[TRANSCRIPTION_SKIPPED_NO_QUOTA]"
_TRANSCRIPTION_SKIPPED_FFMPEG_NOT_FOUND = "[TRANSCRIPTION_SKIPPED_FFMPEG_NOT_FOUND]"


def _create_whisper_transcription(audio_file, source_path: str, chunk_index: int):
    max_retries = int(getattr(settings, "WHISPER_TRANSCRIBE_MAX_RETRIES", _WHISPER_TRANSCRIBE_MAX_RETRIES))
    for attempt in range(max_retries + 1):
        try:
            return client.audio.transcriptions.create(
                model=settings.WHISPER_MODEL,
                file=audio_file,
                language=settings.WHISPER_LANGUAGE,
            )
        except Exception as exc:
            error_text = str(exc).lower()
            if "insufficient_quota" in error_text:
                raise

            retryable = (
                isinstance(exc, InternalServerError)
                or "timeout" in error_text
                or "timed out" in error_text
                or "connection" in error_text
                or "temporarily unavailable" in error_text
                or "rate limit" in error_text
                or "429" in error_text
            )

            if not retryable:
                raise

            if attempt >= max_retries:
                logger.exception(
                    "Whisper retryable error exhausted retries. chunk_index=%s source=%s",
                    chunk_index,
                    source_path,
                )
                return None
            backoff_sec = 2**attempt
            logger.warning(
                "Whisper retryable error. retry_count=%s chunk_index=%s wait_seconds=%s source=%s reason=%s",
                attempt + 1,
                chunk_index,
                backoff_sec,
                source_path,
                exc,
            )
            time.sleep(backoff_sec)


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
            response = _create_whisper_transcription(
                audio_file=audio_file,
                source_path=file_path,
                chunk_index=chunk_index,
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

    if response is None:
        logger.error(
            "Whisper transcription failed after max retries. Continuing with empty transcript: %s",
            file_path,
        )
        return chunk_index, ""

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

    bytes_per_second = (
        _WHISPER_SPLIT_SAMPLE_RATE
        * _WHISPER_SPLIT_CHANNELS
        * _WHISPER_SPLIT_BYTES_PER_SAMPLE
    )
    max_chunk_sec_by_size = max(
        1.0,
        (_WHISPER_MAX_BYTES * _WHISPER_SPLIT_SIZE_SAFETY_MARGIN) / bytes_per_second,
    )
    max_chunk_seconds = float(
        getattr(settings, "WHISPER_MAX_CHUNK_SECONDS", _WHISPER_MAX_CHUNK_SECONDS)
    )
    chunk_sec = min(max_chunk_seconds, max_chunk_sec_by_size, duration_sec)
    logger.info(
        "WHISPER_SPLIT_PLAN: duration_sec=%s total_bytes=%s chunk_sec=%s max_chunk_seconds=%s",
        duration_sec,
        total_bytes,
        chunk_sec,
        max_chunk_seconds,
    )

    chunks_text: List[str] = []
    success_chunks = 0
    failed_chunks = 0
    processed_until_sec = 0.0
    failed_ranges: List[Tuple[float, float]] = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        chunk_index = 0
        offset = 0.0
        while offset < duration_sec:
            chunk_start = float(offset)
            chunk_end = float(min(offset + chunk_sec, duration_sec))
            logger.info(
                "WHISPER_SPLIT_CHUNK_START: chunk_index=%s start_sec=%.2f end_sec=%.2f start_min=%.2f end_min=%.2f",
                chunk_index,
                chunk_start,
                chunk_end,
                chunk_start / 60.0,
                chunk_end / 60.0,
            )
            chunk_path = str(Path(tmp_dir) / f"split_chunk_{chunk_index}.wav")
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-ss", str(offset),
                        "-t", str(chunk_sec),
                        "-i", file_path,
                        "-ac", str(_WHISPER_SPLIT_CHANNELS),
                        "-ar", str(_WHISPER_SPLIT_SAMPLE_RATE),
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
            except subprocess.CalledProcessError as exc:
                stderr_text = ""
                if getattr(exc, "stderr", None):
                    try:
                        stderr_text = exc.stderr.decode("utf-8", errors="ignore")
                    except Exception:
                        stderr_text = str(exc.stderr)
                logger.warning(
                    "ffmpeg split failed. returncode=%s chunk_index=%s offset=%s chunk_sec=%s path=%s stderr=%s",
                    exc.returncode,
                    chunk_index,
                    offset,
                    chunk_sec,
                    file_path,
                    stderr_text[:1000],
                )
                failed_chunks += 1
                failed_ranges.append((chunk_start, chunk_end))
                offset += chunk_sec
                chunk_index += 1
                continue
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
                    response = _create_whisper_transcription(
                        audio_file=f,
                        source_path=chunk_path,
                        chunk_index=chunk_index,
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
            if response is None:
                logger.error(
                    "Whisper split chunk failed after max retries. Skipping chunk_index=%s path=%s",
                    chunk_index,
                    chunk_path,
                )
                failed_chunks += 1
                failed_ranges.append((chunk_start, chunk_end))
                offset += chunk_sec
                chunk_index += 1
                continue
            chunks_text.append(response.text.strip())
            success_chunks += 1
            processed_until_sec = max(processed_until_sec, chunk_end)
            logger.info(
                "WHISPER_SPLIT_CHUNK_DONE: chunk_index=%s processed_until_sec=%.2f processed_until_min=%.2f",
                chunk_index,
                processed_until_sec,
                processed_until_sec / 60.0,
            )
            offset += chunk_sec
            chunk_index += 1

    merged = "\n".join(t for t in chunks_text if t)
    coverage_percent = (processed_until_sec / duration_sec * 100.0) if duration_sec > 0 else 0.0
    logger.info(
        "WHISPER_SPLIT_SUMMARY: total_duration_sec=%.2f total_duration_min=%.2f total_chunks=%s success_chunks=%s failed_chunks=%s processed_until_sec=%.2f processed_until_min=%.2f coverage_percent=%.2f",
        duration_sec,
        duration_sec / 60.0,
        chunk_index,
        success_chunks,
        failed_chunks,
        processed_until_sec,
        processed_until_sec / 60.0,
        coverage_percent,
    )
    if failed_ranges:
        condensed = [f"{s/60.0:.2f}-{e/60.0:.2f}min" for s, e in failed_ranges[:10]]
        logger.warning(
            "WHISPER_SPLIT_FAILED_RANGES: count=%s sample=%s",
            len(failed_ranges),
            ",".join(condensed),
        )
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
