from transcription.whisper_transcribe import transcribe_with_whisper, _get_duration_seconds
from preprocess.transcript_preprocessor_gpt import preprocess_transcript_with_gpt
from minutes.minutes_generator_claude import generate_minutes_with_claude, _normalize_dictionary_items
from dictionary.company_dictionary import load_company_dictionary
from dictionary.abbreviation_dictionary import load_abbreviation_dictionary

path = r"audio/2026_0316_NRE_物流事業部・川口様_部門特化研修について.m4a"
meeting_info = {
    'date': '2026_0316',
    'customer_name': 'NRE',
    'meeting_title': '物流事業部・川口様_部門特化研修について',
}

print(f"AUDIO_DURATION_SEC={_get_duration_seconds(path):.2f}")
print(f"AUDIO_DURATION_MIN={_get_duration_seconds(path)/60.0:.2f}")

transcript = transcribe_with_whisper(path)
print(f"WHISPER_CHAR_COUNT={len(transcript)}")

company = _normalize_dictionary_items(load_company_dictionary())
abbr = _normalize_dictionary_items(load_abbreviation_dictionary())
pre = preprocess_transcript_with_gpt(
    transcript=transcript,
    company_dictionary=company,
    abbreviation_dictionary=abbr,
)
print(f"PREPROCESS_LINE_COUNT={len([l for l in pre.splitlines() if l.strip()])}")

minutes_md = generate_minutes_with_claude(
    transcript=pre,
    meeting_info=meeting_info,
    company_dictionary=company,
    abbreviation_dictionary=abbr,
)
print(f"MINUTES_CHAR_COUNT={len(minutes_md)}")
print(f"HAS_VERBATIM_HEADER={'## 発言録（逐語）' in minutes_md}")
print(f"TAIL={minutes_md[-120:].replace(chr(10), ' ')}")
