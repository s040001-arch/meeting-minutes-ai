import argparse
import json
import os
import re

from ai_correct_text import call_openai_incorporate_answer, resolve_openai_api_key
from recorrect_with_answer import resolve_transcript_path


def _load_question_result_for_job(job_id: str, input_root: str) -> dict | None:
    path = os.path.join(input_root, job_id, "question_result.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _load_question_text_for_job(job_id: str, input_root: str) -> str:
    data = _load_question_result_for_job(job_id, input_root)
    if not data:
        return ""
    return str(data.get("question_text") or "").strip()


def _quoted_snippets_from_question(question_text: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"「([^」]{4,})」", question_text):
        s = m.group(1).strip()
        if s:
            out.append(s)
    return out


def _anchor_strings_for_span(
    question_result: dict | None, question_text: str
) -> list[str]:
    """selected_unknown.text を最優先し、次に質問内の「…」引用を試す（重複除去）。"""
    seen: set[str] = set()
    ordered: list[str] = []

    def add(s: str) -> None:
        t = s.strip()
        if len(t) < 2 or t in seen:
            return
        seen.add(t)
        ordered.append(t)

    if question_result:
        su = question_result.get("selected_unknown")
        if isinstance(su, dict):
            add(str(su.get("text") or ""))
    for snip in _quoted_snippets_from_question(question_text):
        add(snip)
    return ordered


def _find_first_anchor_span(
    base_text: str, anchors: list[str]
) -> tuple[int, int] | None:
    """先頭一致のみ（1箇所）。戻り値は [start, end) の一致区間。"""
    for a in anchors:
        if not a:
            continue
        i = base_text.find(a)
        if i >= 0:
            return (i, i + len(a))
    return None


def load_answer_record(
    answers_json_path: str,
    answer_index: int = -1,
    question_id: str | None = None,
    job_id: str | None = None,
    input_root: str | None = None,
) -> tuple[dict, str]:
    """
    Returns (record, selection_note).
    job_id + input_root が渡り answer_index==-1 かつ question_id 未指定のときは
    ジョブに紐づく回答を新しい順に探す（従来の「配列末尾だけ」取り違え防止）。
    """
    if not os.path.isfile(answers_json_path):
        raise FileNotFoundError(f"answers json not found: {answers_json_path}")
    with open(answers_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        raise ValueError("answers json must be a non-empty JSON array.")
    records = [x for x in data if isinstance(x, dict)]
    if not records:
        raise ValueError("answers json does not contain valid records.")

    if question_id:
        matched = [r for r in records if str(r.get("question_id", "")) == question_id]
        if not matched:
            raise ValueError(f"no answer found for question_id={question_id}")
        return matched[-1], "question_id_match"

    use_job_scope = (
        job_id
        and input_root
        and answer_index == -1
    )
    if use_job_scope:
        for r in reversed(records):
            if str(r.get("job_id") or "") == job_id:
                return r, "job_id_match"
        expected_q = _load_question_text_for_job(job_id, input_root)
        if expected_q:
            for r in reversed(records):
                if str(r.get("question_text") or "").strip() == expected_q:
                    return r, "question_text_match_fallback"
        any_job = any(str(r.get("job_id") or "").strip() for r in records)
        if not any_job:
            return records[-1], "legacy_global_latest_no_job_id_in_answers"
        raise ValueError(
            f"no answer for job_id={job_id} in {answers_json_path} "
            "(job_id 一致・question_text 一致のいずれもありません)"
        )

    if answer_index < 0:
        answer_index = len(records) + answer_index
    if answer_index < 0 or answer_index >= len(records):
        raise IndexError(f"answer_index out of range: {answer_index}")
    return records[answer_index], "answer_index"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Task 5-4（第二スライス）: webhook保存済みの回答JSONから最新回答を取り、再補正を実行する"
        )
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--answers-json",
        default=os.path.join("data", "line_answers.json"),
        help="webhook回答JSON（デフォルト: data/line_answers.json）",
    )
    parser.add_argument(
        "--answer-index",
        type=int,
        default=-1,
        help="使う回答レコードのインデックス（デフォルト: -1 = 最新）",
    )
    parser.add_argument(
        "--question-id",
        default=None,
        help="指定時はこの question_id の最新回答を使う",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="入力テキスト（未指定時: merged_transcript_ai.txt を優先、なければ merged_transcript.txt）",
    )
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="ジョブディレクトリのルート（デフォルト: data/transcriptions）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="出力先（未指定時: {input_root}/{job_id}/merged_transcript_after_qa.txt）",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="OpenAIモデル名（デフォルト: gpt-4o-mini）",
    )
    parser.add_argument(
        "--openai-timeout-sec",
        type=int,
        default=600,
        help="回答反映APIのHTTPタイムアウト秒（デフォルト: 600）",
    )
    parser.add_argument(
        "--span-before",
        type=int,
        default=1000,
        help="アンカー一致位置より前に含める最大文字数（抜粋API用、デフォルト: 1000）",
    )
    parser.add_argument(
        "--span-after",
        type=int,
        default=1000,
        help="アンカー一致位置より後に含める最大文字数（抜粋API用、デフォルト: 1000）",
    )
    args = parser.parse_args()

    record, selection_note = load_answer_record(
        answers_json_path=args.answers_json,
        answer_index=args.answer_index,
        question_id=args.question_id,
        job_id=args.job_id,
        input_root=args.input_root,
    )
    print(f"recorrect_answer_selection={selection_note}")
    if selection_note.startswith("legacy_"):
        print(
            "recorrect_answer_selection_warning="
            "line_answers に job_id がありません。グローバル最新を使用しています。"
        )
    question_text = str(record.get("question_text") or "").strip()
    answer_text = str(record.get("answer_text") or "").strip()
    if not question_text:
        raise ValueError("selected answer record does not contain question_text.")
    if not answer_text:
        raise ValueError("selected answer record does not contain answer_text.")

    in_path = resolve_transcript_path(args.job_id, args.input, args.input_root)
    if not os.path.isfile(in_path):
        raise FileNotFoundError(f"input file not found: {in_path}")
    with open(in_path, "r", encoding="utf-8") as f:
        base_text = f.read()

    api_key, key_source = resolve_openai_api_key()
    print(f"debug_openai_api_key_found={bool(api_key)}")
    print(f"debug_openai_api_key_source={key_source}")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    question_result = _load_question_result_for_job(args.job_id, args.input_root)
    anchors = _anchor_strings_for_span(question_result, question_text)
    anchor_match = _find_first_anchor_span(base_text, anchors)

    if anchor_match is not None:
        m0, m1 = anchor_match
        span_start = max(0, m0 - max(0, args.span_before))
        span_end = min(len(base_text), m1 + max(0, args.span_after))
        span_text = base_text[span_start:span_end]
        print(
            "recorrect_incorporate_mode=span "
            f"excerpt_chars={len(span_text)} "
            f"anchor_range=[{m0},{m1}) "
            f"span_range=[{span_start},{span_end})"
        )
        updated_span = call_openai_incorporate_answer(
            text=span_text,
            question_text=question_text,
            answer_text=answer_text,
            model=args.model,
            api_key=api_key,
            timeout_sec=args.openai_timeout_sec,
            excerpt_mode=True,
        )
        updated = base_text[:span_start] + updated_span + base_text[span_end:]
    else:
        print("recorrect_incorporate_mode=full anchor_not_found_in_transcript=1")
        updated = call_openai_incorporate_answer(
            text=base_text,
            question_text=question_text,
            answer_text=answer_text,
            model=args.model,
            api_key=api_key,
            timeout_sec=args.openai_timeout_sec,
        )

    out_path = args.output or os.path.join(
        args.input_root, args.job_id, "merged_transcript_after_qa.txt"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(updated)

    print(f"job_id={args.job_id}")
    print(f"answers_json={args.answers_json}")
    print(f"input={in_path}")
    print(f"output={out_path}")
    print(f"question_id={record.get('question_id')}")
    print(f"answer_job_id={record.get('job_id')}")
    print(f"model={args.model}")
    print(f"openai_timeout_sec={args.openai_timeout_sec}")


if __name__ == "__main__":
    main()
