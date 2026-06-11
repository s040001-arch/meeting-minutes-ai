import argparse
import json
import os
import re

from ai_correct_text import call_openai_incorporate_answer, resolve_openai_api_key
from job_context import load_job_context
from recognition_batch import (
    RECOGNITION_BATCH_FORMAT,
    apply_batch_corrections,
    is_coherence_unknown_item,
    parse_batch_answer,
    parse_single_coherence_answer,
)
from transcript_paths import MIN_TRANSCRIPT_LENGTH_RATIO, resolve_transcript_path

# 学習辞書追加で「無回答/わからない」と判断する文言
_NEGATIVE_ANSWER_PATTERNS = (
    "わからない", "分からない", "わかりません", "分かりません",
    "不明", "わすれた", "忘れた", "覚えてない", "おぼえてない",
    "そのまま", "そのままで", "問題ない", "問題なし", "間違ってない",
    "間違いない", "正しい", "ok", "OK",
)
# 単一名詞っぽい回答かを判別する正規表現(句点・読点・接続詞のない短い表現)
_SHORT_NOUN_ANSWER_RE = re.compile(
    r"^[^\s。、！？!?]{1,20}$"
)
# 「正しくは X」「Xです」「Xのこと(を指して?います?)」「X(=表記|の意)」等から X を抽出
_CORRECTION_EXTRACT_PATTERNS = [
    re.compile(r"^正しくは\s*[「『]?([^\s。、！？!?「」『』]{1,20})[」』]?[\s。!]*"),
    re.compile(r"^[「『]([^」』]{1,20})[」』]\s*(?:の(?:こと|意|誤り|誤字)|です)?[\s。!]*$"),
    re.compile(r"^([^\s。、！？!?「」『』]{1,20})\s*(?:のこと|の意|の誤り|の誤字|の音声認識誤り)\s*(?:です|でした)?[\s。!]*$"),
    re.compile(r"^([^\s。、！？!?「」『』]{1,20})\s*です(?:ね|よ)?[\s。!]*$"),
    re.compile(r"^([^\s。、！？!?「」『』]{1,20})\s*でした[\s。!]*$"),
]


def _extract_correction_word_from_answer(answer_text: str) -> str:
    """ユーザ回答から「正しい単語」を抽出する。失敗時は空文字列。

    対応するパターン:
      - そのまま短い名詞: "濃淡"
      - 「○○」形式: "「ご援護」", "『監督』"
      - "正しくは X" : "正しくは濃淡"
      - "X のこと/誤り/音声認識誤り": "監督の音声認識誤り"
      - "X です/でした" : "濃淡です"
    無回答パターンや長文回答(説明調)は抽出失敗としてスキップ。
    """
    if not answer_text:
        return ""
    s = answer_text.strip()
    if not s:
        return ""
    # 否定表現はスキップ
    s_lower_compact = s.replace(" ", "").lower()
    for pat in _NEGATIVE_ANSWER_PATTERNS:
        if pat in s_lower_compact or pat in s:
            return ""
    # 抽出パターン
    for pat in _CORRECTION_EXTRACT_PATTERNS:
        m = pat.match(s)
        if m:
            candidate = m.group(1).strip().strip("「」『』\"'")
            if candidate:
                return candidate
    # 短い単一名詞回答
    if _SHORT_NOUN_ANSWER_RE.match(s):
        return s.strip("「」『』\"'")
    return ""


def _persist_coherence_answer_to_learned_dict(
    *, job_id: str, input_root: str, question_result: dict | None,
    answer_text: str, base_text: str,
    learned_path: str | None = None,
) -> dict:
    """Coherence 由来の質問への回答を学習辞書に追加する。

    返り値: {"action": "added|updated|skipped|noop", "wrong": ..., "right": ..., "reason": ...}
    本処理が失敗してもパイプライン全体は止めない(呼び出し側で warn ログのみ)。
    """
    if not isinstance(question_result, dict):
        return {"action": "noop", "reason": "no_question_result"}
    su = question_result.get("selected_unknown") or {}
    if not isinstance(su, dict):
        return {"action": "noop", "reason": "no_selected_unknown"}
    source = str(su.get("source") or "").strip()
    qtype = str(su.get("type") or "").strip()
    # coherence_review 由来でない質問はスキップ(通常の不明点 Q&A は対象外)
    if source != "coherence_review" and qtype != "coherence_review":
        return {"action": "noop", "reason": "not_coherence_question"}
    wrong = str(su.get("anomaly_word") or "").strip()
    if not wrong:
        return {"action": "noop", "reason": "no_anomaly_word"}
    right = _extract_correction_word_from_answer(answer_text)
    if not right:
        return {
            "action": "skipped",
            "reason": "answer_not_short_correction",
            "wrong": wrong,
        }
    # 例示用の context を本文から抽出
    example = ""
    idx = base_text.find(wrong)
    if idx >= 0:
        start = max(0, idx - 20)
        end = min(len(base_text), idx + len(wrong) + 20)
        example = base_text[start:end].strip()
    try:
        from learned_corrections_store import (
            DEFAULT_LEARNED_PATH,
            add_learned_correction,
        )

        result = add_learned_correction(
            wrong=wrong,
            right=right,
            via="line_qa",
            job_id=job_id,
            example=example,
            confidence="high",
            path=learned_path or DEFAULT_LEARNED_PATH,
        )
        return result
    except Exception as e:  # noqa: BLE001
        return {
            "action": "skipped",
            "reason": f"persist_failed={e!r}",
            "wrong": wrong,
            "right": right,
        }


def _is_coherence_review_question(question_result: dict | None) -> bool:
    if not isinstance(question_result, dict):
        return False
    if _is_recognition_batch_question(question_result):
        return False
    su = question_result.get("selected_unknown")
    if not isinstance(su, dict):
        return False
    return is_coherence_unknown_item(su)


def _parse_coherence_single_answer(answer_text: str, *, word: str) -> dict:
    parsed = parse_single_coherence_answer(answer_text, word=word)
    if parsed.get("action") != "unknown":
        return parsed
    extracted = _extract_correction_word_from_answer(answer_text)
    if extracted:
        return {"word": word, "action": "correct", "correction": extracted}
    return parsed


def _load_unknown_points(job_id: str, input_root: str) -> list[dict]:
    path = os.path.join(input_root, job_id, "unknown_points.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_unknown_points(job_id: str, input_root: str, unknown_points: list[dict]) -> None:
    path = os.path.join(input_root, job_id, "unknown_points.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(unknown_points, f, ensure_ascii=False, indent=2)


def _is_coherence_item(item: dict) -> bool:
    return is_coherence_unknown_item(item)


def _count_unanswered_coherence(job_id: str, input_root: str) -> int:
    return sum(
        1
        for item in _load_unknown_points(job_id, input_root)
        if _is_coherence_item(item)
        and str(item.get("status") or "").strip().lower()
        not in {"answered", "done", "closed", "resolved"}
    )


def _mark_coherence_single_answered(
    *,
    job_id: str,
    input_root: str,
    selected_unknown: dict,
    parsed: dict,
    answer_text: str,
    question_id: str,
) -> int:
    from datetime import datetime, timezone

    unknown_points = _load_unknown_points(job_id, input_root)
    if not unknown_points:
        return 0
    target_id = str(selected_unknown.get("anomaly_id") or "").strip()
    target_word = str(selected_unknown.get("anomaly_word") or parsed.get("word") or "").strip()
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0
    for item in unknown_points:
        status = str(item.get("status") or "").strip().lower()
        if status == "answered" and item.get("correction_action"):
            continue
        item_id = str(item.get("anomaly_id") or "").strip()
        item_word = str(item.get("anomaly_word") or "").strip()
        if target_id and item_id == target_id:
            match = True
        elif target_word and item_word == target_word:
            match = True
        else:
            match = False
        if not match:
            continue
        item["status"] = "answered"
        item["answer"] = answer_text
        item["answered_by_question_id"] = question_id
        item["answered_at"] = now_iso
        item["correction_action"] = str(parsed.get("action") or "unknown")
        item["correction_word"] = str(parsed.get("correction") or "")
        updated += 1
    if updated > 0:
        _save_unknown_points(job_id, input_root, unknown_points)
    return updated


def _build_parsed_from_answered_coherence(job_id: str, input_root: str) -> list[dict]:
    parsed: list[dict] = []
    for item in _load_unknown_points(job_id, input_root):
        if not _is_coherence_item(item):
            continue
        if str(item.get("status") or "").strip().lower() != "answered":
            continue
        word = str(item.get("anomaly_word") or "").strip()
        if not word:
            continue
        action = str(item.get("correction_action") or "").strip().lower()
        correction = str(item.get("correction_word") or "").strip()
        if not action:
            reparsed = _parse_coherence_single_answer(str(item.get("answer") or ""), word=word)
            action = str(reparsed.get("action") or "unknown")
            correction = str(reparsed.get("correction") or "")
        parsed.append(
            {
                "anomaly_id": item.get("anomaly_id", ""),
                "word": word,
                "action": action if action in {"correct", "keep", "unknown"} else "unknown",
                "correction": correction,
            }
        )
    return parsed


def _handle_coherence_single_answer(
    *,
    job_id: str,
    input_root: str,
    question_result: dict,
    answer_text: str,
    question_id: str,
    base_text: str,
    out_path: str,
) -> None:
    su = question_result.get("selected_unknown") or {}
    word = str(su.get("anomaly_word") or "").strip()
    if not word:
        raise ValueError("coherence question missing anomaly_word.")
    parsed_one = _parse_coherence_single_answer(answer_text, word=word)
    _mark_coherence_single_answered(
        job_id=job_id,
        input_root=input_root,
        selected_unknown=su if isinstance(su, dict) else {},
        parsed=parsed_one,
        answer_text=answer_text,
        question_id=question_id,
    )
    try:
        learn_result = _persist_coherence_answer_to_learned_dict(
            job_id=job_id,
            input_root=input_root,
            question_result=question_result,
            answer_text=answer_text,
            base_text=base_text,
        )
        print(f"learned_dict_from_qa={learn_result}")
    except Exception as e:  # noqa: BLE001
        print(f"learned_dict_from_qa_failed={e!r}")

    remaining = _count_unanswered_coherence(job_id, input_root)
    if remaining > 0:
        updated = base_text
        applied: list[dict] = []
        print(
            f"recorrect_incorporate_mode=coherence_deferred "
            f"remaining={remaining} action={parsed_one.get('action')}"
        )
    else:
        all_parsed = _build_parsed_from_answered_coherence(job_id, input_root)
        updated, applied = apply_batch_corrections(base_text, all_parsed)
        learned = _persist_batch_corrections_to_learned_dict(
            job_id=job_id, applied=applied, base_text=base_text
        )
        print(
            f"recorrect_incorporate_mode=coherence_batch_apply "
            f"items={len(all_parsed)} applied={len(applied)} learned_added={learned}"
        )

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(updated)


def _is_recognition_batch_question(question_result: dict | None) -> bool:
    if not isinstance(question_result, dict):
        return False
    if str(question_result.get("question_format") or "").strip() == RECOGNITION_BATCH_FORMAT:
        return True
    su = question_result.get("selected_unknown")
    return isinstance(su, dict) and bool(su.get("batch_items"))


def _mark_batch_items_answered_in_unknowns(
    *,
    job_id: str,
    input_root: str,
    parsed: list[dict],
    answer_text: str,
    question_id: str,
) -> int:
    """バッチで確認した coherence 項目に status を反映する。

    action が correct/keep の項目は answered とし、unknown はそのまま open に残す
    (後続サイクルで再度バッチに載せて聞き直せるようにする)。
    """
    path = os.path.join(input_root, job_id, "unknown_points.json")
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        unknown_points = [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return 0
    resolved_ids = {
        str(p.get("anomaly_id") or "").strip()
        for p in parsed
        if str(p.get("action") or "") in {"correct", "keep"}
    }
    resolved_ids.discard("")
    resolved_words = {
        str(p.get("word") or "").strip()
        for p in parsed
        if str(p.get("action") or "") in {"correct", "keep"}
    }
    resolved_words.discard("")
    if not resolved_ids and not resolved_words:
        return 0
    updated = 0
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    for item in unknown_points:
        if str(item.get("status", "")).strip().lower() == "answered":
            continue
        item_id = str(item.get("anomaly_id") or "").strip()
        item_word = str(item.get("anomaly_word") or "").strip()
        if (item_id and item_id in resolved_ids) or (item_word and item_word in resolved_words):
            item["status"] = "answered"
            item["answer"] = answer_text
            item["answered_by_question_id"] = question_id
            item["answered_at"] = now_iso
            updated += 1
    if updated > 0:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(unknown_points, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    return updated


def _persist_batch_corrections_to_learned_dict(
    *, job_id: str, applied: list[dict], base_text: str
) -> int:
    """バッチ補正で確定した (誤, 正) を学習辞書に追加(次ジョブから機械補正で自動置換)。"""
    corrections = [a for a in applied if str(a.get("action")) == "correct"]
    if not corrections:
        return 0
    try:
        from learned_corrections_store import DEFAULT_LEARNED_PATH, add_learned_correction
    except Exception as e:  # noqa: BLE001
        print(f"learned_corrections_import_failed={e!r}")
        return 0
    persisted = 0
    for a in corrections:
        wrong = str(a.get("before") or "").strip()
        right = str(a.get("after") or "").strip()
        if not wrong or not right or wrong == right:
            continue
        example = ""
        idx = base_text.find(wrong)
        if idx >= 0:
            start = max(0, idx - 20)
            end = min(len(base_text), idx + len(wrong) + 20)
            example = base_text[start:end].strip()
        try:
            result = add_learned_correction(
                wrong=wrong, right=right, via="line_qa", job_id=job_id,
                example=example, confidence="high", path=DEFAULT_LEARNED_PATH,
            )
            if result.get("action") in ("added", "updated"):
                persisted += 1
        except Exception as e:  # noqa: BLE001
            print(f"learned_corrections_add_failed wrong={wrong!r} err={e!r}")
    return persisted


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
    """先頭一致のみ（1箇所）。完全一致を優先し、長いアンカーは部分一致も試す。"""
    for a in anchors:
        if not a:
            continue
        i = base_text.find(a)
        if i >= 0:
            return (i, i + len(a))

    for a in sorted(anchors, key=len, reverse=True):
        if len(a) < 24:
            continue
        for ratio in (0.75, 0.5):
            short_len = max(20, int(len(a) * ratio))
            short = a[:short_len].strip()
            if len(short) < 20:
                continue
            i = base_text.find(short)
            if i >= 0:
                return (i, i + len(short))
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
        default="gpt-4.1",
        help="OpenAIモデル名（デフォルト: gpt-4.1）",
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
        default=400,
        help="アンカー一致位置より前に含める最大文字数（抜粋API用、デフォルト: 400）",
    )
    parser.add_argument(
        "--span-after",
        type=int,
        default=400,
        help="アンカー一致位置より後に含める最大文字数（抜粋API用、デフォルト: 400）",
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

    out_path = args.output or os.path.join(
        args.input_root, args.job_id, "merged_transcript_after_qa.txt"
    )

    # 認識ゆれ(1 語 1 問): 回答を記録し、全件回答後に一括補正。
    if _is_coherence_review_question(question_result):
        _handle_coherence_single_answer(
            job_id=args.job_id,
            input_root=args.input_root,
            question_result=question_result,
            answer_text=answer_text,
            question_id=str(record.get("question_id") or ""),
            base_text=base_text,
            out_path=out_path,
        )
        print(f"job_id={args.job_id}")
        print(f"answers_json={args.answers_json}")
        print(f"input={in_path}")
        print(f"output={out_path}")
        print(f"question_id={record.get('question_id')}")
        print(f"answer_job_id={record.get('job_id')}")
        return

    # レガシー: 認識ゆれの一括確認(バッチ)への回答。
    if _is_recognition_batch_question(question_result):
        out_path = args.output or os.path.join(
            args.input_root, args.job_id, "merged_transcript_after_qa.txt"
        )
        batch_items = (question_result.get("selected_unknown") or {}).get("batch_items") or []
        parsed = parse_batch_answer(
            answer_text=answer_text,
            items=batch_items,
            api_key=api_key,
            model=args.model,
            timeout_sec=args.openai_timeout_sec,
        )
        updated, applied = apply_batch_corrections(base_text, parsed)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(updated)
        answered = _mark_batch_items_answered_in_unknowns(
            job_id=args.job_id,
            input_root=args.input_root,
            parsed=parsed,
            answer_text=answer_text,
            question_id=str(record.get("question_id") or ""),
        )
        learned = _persist_batch_corrections_to_learned_dict(
            job_id=args.job_id, applied=applied, base_text=base_text
        )
        print(f"recorrect_incorporate_mode=recognition_batch items={len(batch_items)}")
        print(f"recognition_batch_applied={len(applied)} answered_marked={answered} learned_added={learned}")
        print(f"job_id={args.job_id}")
        print(f"input={in_path}")
        print(f"output={out_path}")
        print(f"question_id={record.get('question_id')}")
        return

    anchors = _anchor_strings_for_span(question_result, question_text)
    scope_quotes = _quoted_snippets_from_question(question_text)
    if not scope_quotes and question_result:
        su = question_result.get("selected_unknown")
        if isinstance(su, dict):
            t = str(su.get("text") or "").strip()
            if t:
                scope_quotes = [t]
    job_context = load_job_context(os.path.join(args.input_root, args.job_id))
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
            scope_quotes=scope_quotes,
            job_context=job_context,
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
            scope_quotes=scope_quotes,
            job_context=job_context,
        )
        base_len = len(base_text.strip())
        updated_len = len(updated.strip())
        ratio = updated_len / max(base_len, 1)
        if base_len >= 500 and ratio < MIN_TRANSCRIPT_LENGTH_RATIO:
            print(
                "[WARNING] recorrect_from_line_answer: incorporate output too short "
                f"(ratio={ratio:.3f} < {MIN_TRANSCRIPT_LENGTH_RATIO}); "
                "keeping original transcript unchanged"
            )
            updated = base_text

    out_path = args.output or os.path.join(
        args.input_root, args.job_id, "merged_transcript_after_qa.txt"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(updated)

    # Phase 1 学習: coherence 由来の質問だったら、回答から正しい単語を抽出して
    # 学習辞書に追加(次ジョブから機械補正で自動置換される)。非致命。
    try:
        learn_result = _persist_coherence_answer_to_learned_dict(
            job_id=args.job_id,
            input_root=args.input_root,
            question_result=question_result,
            answer_text=answer_text,
            base_text=base_text,
        )
        print(f"learned_dict_from_qa={learn_result}")
    except Exception as e:  # noqa: BLE001
        print(f"learned_dict_from_qa_failed={e!r}")

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
