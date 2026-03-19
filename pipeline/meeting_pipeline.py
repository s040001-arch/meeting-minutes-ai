from pathlib import Path
import os

from audio.audio_loader import load_audio_file
from config.settings import settings
from dictionary.abbreviation_dictionary import load_abbreviation_dictionary
from dictionary.company_dictionary import load_company_dictionary
from docs.google_docs_writer import write_minutes_to_google_docs
from minutes.minutes_formatter import format_minutes
from minutes.minutes_generator_claude import generate_minutes_with_claude
from preprocess.transcript_preprocessor_gpt import (
    detect_ambiguity_questions,
    preprocess_transcript_with_gpt,
)
from transcription.whisper_transcribe import transcribe_with_whisper
from utils.file_parser import parse_audio_filename
from utils.logger import get_logger

logger = get_logger(__name__)


def _get_process_rss_mb() -> float | None:
    try:
        import resource

        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(float(rss_kb) / 1024.0, 2)
    except Exception:
        return None


def _log_memory_usage(stage: str) -> None:
    rss_mb = _get_process_rss_mb()
    if rss_mb is None:
        logger.info("PIPELINE_MEMORY stage=%s rss_mb=unknown pid=%s", stage, os.getpid())
        return
    logger.info("PIPELINE_MEMORY stage=%s rss_mb=%s pid=%s", stage, rss_mb, os.getpid())


def run_meeting_pipeline(audio_file_path: str) -> dict:
    logger.info("Start meeting pipeline: %s", audio_file_path)

    meeting_info = parse_audio_filename(audio_file_path)
    filename_fallback_used = False
    if not meeting_info:
        filename_fallback_used = True
        file_stem = Path(audio_file_path).stem
        logger.warning(
            "Invalid audio filename format, continue with fallback meeting info: %s",
            audio_file_path,
        )
        meeting_info = {
            "date": "0000_0000",
            "customer_name": "unknown",
            "meeting_title": file_stem or "meeting",
        }

    audio_path = load_audio_file(audio_file_path)
    logger.info("Audio loaded: %s", audio_path)

    transcript = transcribe_with_whisper(audio_path)
    logger.info("Whisper transcription completed.")
    _log_memory_usage("post_whisper")
    whisper_fallback_used = "[TRANSCRIPTION_SKIPPED_NO_QUOTA]" in str(transcript)

    company_dictionary = load_company_dictionary()
    abbreviation_dictionary = load_abbreviation_dictionary()
    logger.info(
        "Dictionaries loaded. company=%s abbreviation=%s",
        len(company_dictionary) if company_dictionary else 0,
        len(abbreviation_dictionary) if abbreviation_dictionary else 0,
    )

    speaker_labeling_config = settings.get_speaker_labeling_config()
    gpt_speaker_rule_prompt_block = settings.get_gpt_speaker_rule_prompt_block()

    logger.info(
        "Speaker labeling config loaded. enabled=%s heuristic_threshold=%s force_assign=%s customer_threshold=%s precena_threshold=%s ambiguous_threshold=%s",
        speaker_labeling_config.get("enabled"),
        speaker_labeling_config.get("heuristic_threshold"),
        speaker_labeling_config.get("force_assign"),
        speaker_labeling_config.get("customer_threshold"),
        speaker_labeling_config.get("precena_threshold"),
        speaker_labeling_config.get("ambiguous_threshold"),
    )
    _log_memory_usage("pre_gpt_preprocess")

    labeled_transcript = preprocess_transcript_with_gpt(
        transcript=transcript,
        company_dictionary=company_dictionary,
        abbreviation_dictionary=abbreviation_dictionary,
        enable_speaker_labeling=speaker_labeling_config.get("enabled", True),
        speaker_labeling_config=speaker_labeling_config,
        speaker_rule_prompt_block=gpt_speaker_rule_prompt_block,
    )
    logger.info("GPT transcript preprocessing completed.")
    _log_memory_usage("post_gpt_preprocess")

    ambiguity_questions = detect_ambiguity_questions(
        cleaned_transcript=labeled_transcript,
        file_client_name=str(meeting_info.get("customer_name") or ""),
    )[:3]
    logger.info("Ambiguity questions detected: %s", len(ambiguity_questions))

    minutes_result = generate_minutes_with_claude(
        transcript=labeled_transcript,
        meeting_info=meeting_info,
        company_dictionary=company_dictionary,
        abbreviation_dictionary=abbreviation_dictionary,
    )
    logger.info("Claude minutes generation completed.")
    if isinstance(minutes_result, str):
        formatted_minutes = minutes_result
        claude_fallback_used = "[MINUTES_GENERATION_SKIPPED_CLAUDE_AUTH_ERROR]" in minutes_result
    else:
        claude_fallback_used = (
            str(minutes_result.get("meeting_summary", "")).strip()
            == "[MINUTES_GENERATION_SKIPPED_CLAUDE_AUTH_ERROR]"
        )
        formatted_minutes = format_minutes(minutes_result)

    logger.info("Minutes formatting completed.")

    logger.info(
        "PIPELINE_GOOGLE_DOCS_SAVE_CALL: enabled=%s meeting=%s",
        getattr(settings, "ENABLE_GOOGLE_DOCS_WRITE", True),
        f"{meeting_info.get('date', '')}_{meeting_info.get('customer_name', '')}_{meeting_info.get('meeting_title', '')}",
    )
    google_doc_result = write_minutes_to_google_docs(
        meeting_info=meeting_info,
        minutes_text=formatted_minutes,
        audio_file_path=audio_file_path,
    )
    logger.info("Google Docs write completed.")
    google_docs_fallback_used = (
        str(google_doc_result.get("document_id", "")).strip()
        == "DUMMY_DOC_PERMISSION_DENIED"
    )
    logger.info(
        "PIPELINE_GOOGLE_DOCS_SAVE_RESULT: google_docs_save=%s fallback=%s document_id=%s",
        not google_docs_fallback_used,
        google_docs_fallback_used,
        str(google_doc_result.get("document_id", "")),
    )

    local_minutes_path: str = ""
    try:
        docs_output_dir = Path(__file__).resolve().parent.parent / "docs_output"
        docs_output_dir.mkdir(parents=True, exist_ok=True)
        local_md_name = (
            f"{meeting_info.get('date', '0000_0000')}"
            f"_{meeting_info.get('customer_name', 'unknown')}"
            f"_{meeting_info.get('meeting_title', 'meeting')}.md"
        )
        local_md_path = docs_output_dir / local_md_name
        local_md_path.write_text(formatted_minutes, encoding="utf-8")
        local_minutes_path = str(local_md_path)
        logger.info("Local minutes saved: %s", local_md_path)
    except Exception as exc:
        logger.warning("Failed to save local minutes file: %s", exc)

    return {
        "meeting_info": meeting_info,
        "transcript": transcript,
        "labeled_transcript": labeled_transcript,
        "speaker_labeling_config": speaker_labeling_config,
        "ambiguity_questions": ambiguity_questions,
        "minutes_json": minutes_result,
        "formatted_minutes": formatted_minutes,
        "google_docs_url": google_doc_result["document_url"],
        "local_minutes_path": local_minutes_path,
        "utterance_validations": [],
        "google_doc_result": google_doc_result,
        "execution_summary": {
            "filename_fallback_used": filename_fallback_used,
            "whisper_fallback_used": whisper_fallback_used,
            "claude_fallback_used": claude_fallback_used,
            "google_docs_fallback_used": google_docs_fallback_used,
            "all_real_processing": not (
                filename_fallback_used
                or whisper_fallback_used
                or claude_fallback_used
                or google_docs_fallback_used
            ),
        },
    }
