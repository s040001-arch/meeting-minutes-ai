from transcription.whisper_transcribe import transcribe_with_whisper, _get_duration_seconds
from preprocess.transcript_preprocessor_gpt import preprocess_transcript_with_gpt
from minutes.minutes_generator_claude import _build_minutes_prompt, _normalize_dictionary_items
from dictionary.company_dictionary import load_company_dictionary
from dictionary.abbreviation_dictionary import load_abbreviation_dictionary
from config.settings import settings
from openai import OpenAI

path = r"audio/2026_0316_NRE_物流事業部・川口様_部門特化研修について.m4a"
meeting_info = {
    'date': '2026_0316',
    'customer_name': 'NRE',
    'meeting_title': '物流事業部・川口様_部門特化研修について',
}

duration = _get_duration_seconds(path)
print(f"AUDIO_DURATION_SEC={duration:.2f}")
print(f"AUDIO_DURATION_MIN={duration/60.0:.2f}")

transcript = transcribe_with_whisper(path)
print(f"WHISPER_CHAR_COUNT={len(transcript)}")

company = _normalize_dictionary_items(load_company_dictionary())
abbr = _normalize_dictionary_items(load_abbreviation_dictionary())
pre = preprocess_transcript_with_gpt(
    transcript=transcript,
    company_dictionary=company,
    abbreviation_dictionary=abbr,
)
pre_lines = [l for l in pre.splitlines() if l.strip()]
print(f"PREPROCESS_LINE_COUNT={len(pre_lines)}")

prompt = _build_minutes_prompt(
    transcript=pre,
    meeting_info=meeting_info,
    company_dictionary=company,
    abbreviation_dictionary=abbr,
)

client = OpenAI(api_key=settings.OPENAI_API_KEY)
response = client.responses.create(
    model=getattr(settings, 'OPENAI_GPT_PREPROCESS_MODEL', 'gpt-4.1-mini'),
    temperature=float(getattr(settings, 'CLAUDE_TEMPERATURE', 0)),
    max_output_tokens=int(getattr(settings, 'CLAUDE_MAX_TOKENS', 4000)),
    input=prompt,
)

status = getattr(response, 'status', None)
incomplete = getattr(response, 'incomplete_details', None)
output_text = getattr(response, 'output_text', '') or ''
print(f"RESPONSE_STATUS={status}")
print(f"INCOMPLETE_DETAILS={incomplete}")
print(f"OUTPUT_CHAR_COUNT={len(output_text)}")
print(f"OUTPUT_TAIL={output_text[-120:].replace(chr(10), ' ')}")
