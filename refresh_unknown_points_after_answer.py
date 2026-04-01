import argparse
import json
import os

import requests

from ai_correct_text import resolve_openai_api_key
from extract_unknown_points import extract_unknown_points
from recorrect_from_line_answer import load_answer_record


def _load_unknown_points(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_unknown_points(path: str, unknown_points: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(unknown_points, f, ensure_ascii=False, indent=2)


def _normalize_unknown(item: dict, *, source: str, default_status: str = "open") -> dict:
    out = {
        "type": str(item.get("type") or "").strip(),
        "text": str(item.get("text") or "").strip(),
        "reason": str(item.get("reason") or "").strip(),
        "source": str(item.get("source") or source).strip() or source,
        "status": str(item.get("status") or default_status).strip() or default_status,
    }
    if isinstance(item.get("answer"), str) and item["answer"].strip():
        out["answer"] = item["answer"].strip()
    if isinstance(item.get("answered_at"), str) and item["answered_at"].strip():
        out["answered_at"] = item["answered_at"].strip()
    if isinstance(item.get("answered_by_question_id"), str) and item["answered_by_question_id"].strip():
        out["answered_by_question_id"] = item["answered_by_question_id"].strip()
    if isinstance(item.get("context"), str) and item["context"].strip():
        out["context"] = item["context"].strip()
    if isinstance(item.get("hypothesis"), str) and item["hypothesis"].strip():
        out["hypothesis"] = item["hypothesis"].strip()
    return out


def _dedupe_unknown_points(unknown_points: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in unknown_points:
        key = (
            str(item.get("type") or "").strip(),
            str(item.get("text") or "").strip(),
        )
        if not key[1] or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _extract_output_text(result: dict) -> str:
    texts: list[str] = []
    for item in result.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                texts.append(str(content.get("text") or ""))
    return "\n".join(t for t in texts if t).strip()


def _filter_unresolved_candidates_with_llm(
    *,
    transcript_text: str,
    question_text: str,
    answer_text: str,
    candidates: list[dict],
    model: str,
    api_key: str,
    timeout_sec: int,
) -> list[dict]:
    if not candidates:
        return []
    url = "https://api.openai.com/v1/responses"
    transcript = transcript_text.strip()
    if len(transcript) > 12000:
        transcript = transcript[:12000]
    candidate_payload = [
        {
            "type": str(item.get("type") or "").strip(),
            "text": str(item.get("text") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
        }
        for item in candidates
    ]
    system_prompt = (
        "あなたは議事録の確認アシスタントです。"
        "最新の質問と回答がすでに本文へ反映された前提で、"
        "候補の unknown_points のうち、まだ追加質問が必要なものだけを残してください。"
        "回答で周辺文脈まで実質解決した候補は落として構いません。"
        "ただし少しでも不安が残るなら残してください。"
        "単語単位の厳密さより、逐語録全体の意味が通るかを優先します。"
        "出力は JSON 配列のみ。各要素は入力候補から type/text/reason をそのまま返してください。"
    )
    user_payload = {
        "question_text": question_text,
        "answer_text": answer_text,
        "transcript_after_answer": transcript,
        "candidates": candidate_payload,
    }
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0.1,
            "max_output_tokens": 2000,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        },
        timeout=timeout_sec,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"OpenAI API error: status={resp.status_code} body={resp.text[:500]}"
        )
    content = _extract_output_text(resp.json())
    if not content:
        raise RuntimeError("OpenAI response did not contain output_text.")
    parsed = json.loads(content)
    if not isinstance(parsed, list):
        raise RuntimeError("LLM unknown filter output is not an array.")
    allowed = {
        (
            str(item.get("type") or "").strip(),
            str(item.get("text") or "").strip(),
        )
        for item in candidate_payload
    }
    filtered: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_unknown(item, source="regex_after_answer", default_status="open")
        key = (normalized["type"], normalized["text"])
        if key in allowed:
            filtered.append(normalized)
    return _dedupe_unknown_points(filtered)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LINE 回答後に unknown_points.json を再評価して更新する"
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument("--answers-json", default=os.path.join("data", "line_answers.json"))
    parser.add_argument("--answer-index", type=int, default=-1)
    parser.add_argument("--question-id", default=None)
    parser.add_argument("--input", default=None, help="回答反映済みテキスト")
    parser.add_argument("--unknowns", default=None, help="既存 unknown_points.json")
    parser.add_argument("--output", default=None, help="更新後 unknown_points.json")
    parser.add_argument("--llm-model", default="gpt-4.1")
    parser.add_argument("--llm-timeout-sec", type=int, default=180)
    args = parser.parse_args()

    job_dir = os.path.join(args.input_root, args.job_id)
    transcript_path = args.input or os.path.join(job_dir, "merged_transcript_after_qa.txt")
    if not os.path.isfile(transcript_path):
        raise FileNotFoundError(f"transcript file not found: {transcript_path}")

    unknowns_path = args.unknowns or os.path.join(job_dir, "unknown_points.json")
    output_path = args.output or unknowns_path
    existing_unknowns = _load_unknown_points(unknowns_path)
    answered_unknowns = [
        _normalize_unknown(item, source="existing_unknown_points", default_status="answered")
        for item in existing_unknowns
        if str(item.get("status") or "").strip().lower() == "answered"
    ]

    record, selection_note = load_answer_record(
        answers_json_path=args.answers_json,
        answer_index=args.answer_index,
        question_id=args.question_id,
        job_id=args.job_id,
        input_root=args.input_root,
    )
    question_text = str(record.get("question_text") or "").strip()
    answer_text = str(record.get("answer_text") or "").strip()
    if not question_text or not answer_text:
        raise ValueError("selected answer record must contain question_text and answer_text")

    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript_text = f.read()

    extracted_unknowns_raw = extract_unknown_points(transcript_text)
    extracted_unknowns = [
        _normalize_unknown(item, source="regex_after_answer", default_status="open")
        for item in extracted_unknowns_raw
    ]

    api_key, key_source = resolve_openai_api_key()
    print(f"refresh_unknown_points_answer_selection={selection_note}")
    print(f"refresh_unknown_points_openai_api_key_found={bool(api_key)} source={key_source}")

    pending_unknowns = extracted_unknowns
    if api_key and extracted_unknowns:
        try:
            pending_unknowns = _filter_unresolved_candidates_with_llm(
                transcript_text=transcript_text,
                question_text=question_text,
                answer_text=answer_text,
                candidates=extracted_unknowns,
                model=args.llm_model,
                api_key=api_key,
                timeout_sec=args.llm_timeout_sec,
            )
            print(
                "refresh_unknown_points_llm_filter="
                f"applied before={len(extracted_unknowns)} after={len(pending_unknowns)}"
            )
        except Exception as e:
            print(f"refresh_unknown_points_llm_filter_failed={e!r}")

    final_unknowns = _dedupe_unknown_points(answered_unknowns + pending_unknowns)
    _save_unknown_points(output_path, final_unknowns)

    print(f"job_id={args.job_id}")
    print(f"transcript={transcript_path}")
    print(f"unknowns_input={unknowns_path}")
    print(f"unknowns_output={output_path}")
    print(f"answered_count={len(answered_unknowns)}")
    print(f"pending_count={len(pending_unknowns)}")
    print(f"total_count={len(final_unknowns)}")


if __name__ == "__main__":
    main()
