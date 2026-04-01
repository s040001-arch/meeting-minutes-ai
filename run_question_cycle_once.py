import argparse
import json
import os
import uuid
from datetime import datetime, timezone

import requests

from ai_correct_text import resolve_openai_api_key
from generate_one_question import TYPE_PRIORITY, load_unknown_points
from line_send_question import build_line_message, push_line_message
from question_value_selection import (
    deduplicate_unknown_points_by_type_text,
    format_top_candidates_debug,
    pop_value_fields,
    select_one_unknown_value_based,
)
from repo_env import load_dotenv_local

LINE_PENDING_CONTEXT_PATH = os.path.join("data", "line_pending_context.json")


def resolve_context_text_path(job_id: str, input_root: str, explicit_path: str | None) -> str | None:
    if explicit_path:
        return explicit_path if os.path.isfile(explicit_path) else None
    job_dir = os.path.join(input_root, job_id)
    for name in (
        "merged_transcript_after_qa.txt",
        "merged_transcript_ai.txt",
        "merged_transcript.txt",
    ):
        p = os.path.join(job_dir, name)
        if os.path.isfile(p):
            return p
    return None


def _load_doc_url(job_dir: str) -> str:
    path = os.path.join(job_dir, "google_doc_hub.json")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return ""
        return str(data.get("doc_url") or "").strip()
    except (OSError, json.JSONDecodeError):
        return ""


RISKY_TYPE_PRIORITY = {
    "proper_noun_candidate": "固有名詞",
    "organization_candidate": "固有名詞",
    "service_candidate": "固有名詞",
    "suspicious_word": "固有名詞",
    "suspicious_number_or_role": "数値",
}


def _build_unknown_points_compact(unknown_points: list[dict], limit: int = 25) -> list[dict]:
    out: list[dict] = []
    for item in unknown_points[:limit]:
        out.append(
            {
                "type": str(item.get("type", "")).strip(),
                "text": str(item.get("text", "")).strip()[:220],
                "reason": str(item.get("reason", "")).strip()[:220],
            }
        )
    return out


def _is_answered_unknown(item: dict) -> bool:
    status = str(item.get("status", "")).strip().lower()
    if status in {"answered", "done", "closed", "resolved"}:
        return True
    answer = item.get("answer")
    if isinstance(answer, str) and answer.strip():
        return True
    return False


def _filter_pending_unknown_points(unknown_points: list[dict]) -> tuple[list[dict], dict]:
    pending: list[dict] = []
    answered_count = 0
    for item in unknown_points:
        if not isinstance(item, dict):
            continue
        if _is_answered_unknown(item):
            answered_count += 1
            continue
        pending.append(item)
    meta = {
        "unknown_points_count_before_filter": len(unknown_points),
        "answered_unknown_points_count": answered_count,
        "pending_unknown_points_count": len(pending),
    }
    return pending, meta


def _generate_one_question_by_ai(
    *,
    full_text: str,
    unknown_points: list[dict],
    model: str,
    api_key: str,
    timeout_sec: int = 180,
) -> dict:
    """
    AI主導で「全体文脈に最も効く1問」を生成する。
    戻り値は dict:
      - question_status: generated / none
      - question_text
      - selected_unknown
      - selection_audit
      - message
    """
    url = "https://api.openai.com/v1/responses"
    compact_unknowns = _build_unknown_points_compact(unknown_points)
    transcript = (full_text or "").strip()
    if len(transcript) > 12000:
        transcript = transcript[:12000]

    schema_hint = {
        "question_status": "generated|none",
        "question_text": "string",
        "selected_unknown": {"type": "string", "text": "string", "reason": "string"},
        "selection_audit": {
            "selection_mode": "ai_global_context",
            "why_this_question": "string",
            "resolved_if_answered": "string",
            "confidence": "low|medium|high",
        },
        "message": "string",
    }
    user_payload = {
        "transcript": transcript,
        "unknown_points": compact_unknowns,
        "output_schema": schema_hint,
    }
    system_prompt = (
        "あなたは議事録品質管理アシスタントです。"
        "目的は、全文の文脈が最も繋がるように、質問は常に1問だけ選ぶことです。"
        "細かな表記ゆれより、全体の解釈が分岐する論点を優先してください。"
        "質問の結果で本文全体がどこまで確定するかを重視してください。"
        "question_text は LINE 送信用の自然な日本語にしてください。"
        "question_text は1つの確認事項だけを短く聞いてください。"
        "question_text は原則1〜2文、できれば120文字以内にしてください。"
        "question_text に job_id や question_id のような内部IDを書かないでください。"
        "selected_unknown.text は引用用の候補であり、question_text に長文引用をそのまま入れないでください。"
        "不確実性が低く、そのまま提出可能と判断できる場合のみ question_status=none を返してください。"
        "出力は必ずJSONオブジェクトのみ。説明文を付けないでください。"
    )
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_output_tokens": 1600,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout_sec,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI API error: status={resp.status_code} body={resp.text[:500]}")
    result = resp.json()
    output_items = result.get("output", [])
    texts: list[str] = []
    for item in output_items:
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                texts.append(c.get("text", ""))
    content = "\n".join(t for t in texts if t).strip()
    if not content:
        raise RuntimeError("OpenAI response did not contain output_text.")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"AI question JSON parse failed: {e}") from e
    if not isinstance(parsed, dict):
        raise RuntimeError("AI question output is not an object.")
    return parsed


def select_one_unknown_prioritized(unknown_points: list[dict]) -> tuple[dict, dict]:
    if not unknown_points:
        raise ValueError("unknown points is empty.")

    def key_func(idx_item: tuple[int, dict]) -> tuple[int, int, int]:
        idx, item = idx_item
        raw_type = str(item.get("type", ""))
        mapped = RISKY_TYPE_PRIORITY.get(raw_type, raw_type)
        # risky_terms系を先に評価
        risky_band = 0 if raw_type in RISKY_TYPE_PRIORITY else 1
        base_priority = TYPE_PRIORITY.get(mapped, 999)
        # 同順位は入力順を維持
        return (risky_band, base_priority, idx)

    idx, selected = min(enumerate(unknown_points), key=key_func)
    raw_type = str(selected.get("type", ""))
    risky_band = 0 if raw_type in RISKY_TYPE_PRIORITY else 1
    mapped = RISKY_TYPE_PRIORITY.get(raw_type, raw_type)
    base_priority = TYPE_PRIORITY.get(mapped, 999)
    audit = {
        "index_in_unknown_points": idx,
        "unknown_points_count": len(unknown_points),
        "type_raw": raw_type,
        "risky_band": risky_band,
        "type_priority_rank": base_priority,
    }
    return selected, audit


def write_line_pending_context(
    job_id: str,
    question_id: str,
    question_text: str,
    selected_unknown: dict,
    selection_audit: dict,
) -> None:
    """webhook が回答に job_id を付与できるよう、直近の質問コンテキストを共有ファイルへ書く。"""
    payload = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "question_id": question_id,
        "question_text": question_text,
        "selected_unknown": selected_unknown,
        "selection_audit": selection_audit,
    }
    os.makedirs(os.path.dirname(LINE_PENDING_CONTEXT_PATH) or ".", exist_ok=True)
    with open(LINE_PENDING_CONTEXT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _sync_line_pending_to_remote(payload)


def _sync_line_pending_to_remote(payload: dict) -> None:
    """
    Webhook が Railway など別ホストで動くとき、ローカルの JSON は届かない。
    LINE_PENDING_SYNC_URL があれば同一ペイロードを POST し、返信時に job_id を紐づける。
    """
    url = os.getenv("LINE_PENDING_SYNC_URL", "").strip()
    if not url:
        return
    secret = os.getenv("LINE_PENDING_SYNC_SECRET", "").strip()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code != 200:
            print(f"line_pending_sync_http_error status={r.status_code} body={r.text[:500]}")
    except Exception as e:
        print(f"line_pending_sync_failed={e}")


def build_user_friendly_question(selected: dict) -> str:
    target_type = str(selected.get("type", "")).strip()

    ask_line = {
        "service_candidate": "サービス名や呼び方があいまいです。正しい名称を教えてください。",
        "organization_candidate": "会社名・組織名があいまいです。正しい名称を教えてください。",
        "proper_noun_candidate": "人名や固有名詞の表記があいまいです。正しい表記を教えてください。",
        "suspicious_word": "語句の誤変換がありそうです。正しい表現を教えてください。",
    }.get(target_type, "この部分の正しい表現を教えてください。")
    return ask_line


def _normalize_question_text(text: str) -> str:
    s = " ".join(str(text or "").strip().split())
    if not s:
        return ""
    s = s.replace("確認したいこと:", "").replace("確認したいこと：", "").strip()
    if len(s) > 160:
        s = s[:159].rstrip() + "…"
    return s


def main() -> None:
    load_dotenv_local()
    parser = argparse.ArgumentParser(
        description=(
            "Task 5 接続スライス: unknown_points から質問を1件生成し、"
            "LINE送信内容を作成（必要時のみ送信）"
        )
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="ジョブディレクトリのルート（デフォルト: data/transcriptions）",
    )
    parser.add_argument(
        "--unknowns",
        default=None,
        help="不明箇所JSON（未指定時: {input_root}/{job_id}/unknown_points.json）",
    )
    parser.add_argument(
        "--text",
        default=None,
        help="文脈抽出用テキスト（未指定時: after_qa -> ai -> merged の順に探索）",
    )
    parser.add_argument(
        "--question-output",
        default=None,
        help="質問生成結果JSON（未指定時: {input_root}/{job_id}/question_result.json）",
    )
    parser.add_argument(
        "--message-output",
        default=None,
        help="LINE送信用メッセージ保存先（未指定時: {input_root}/{job_id}/question_message.txt）",
    )
    parser.add_argument(
        "--send-line",
        action="store_true",
        help="指定時のみ LINE API へ push する（未指定時は送信しない）",
    )
    parser.add_argument(
        "--debug-selection",
        action="store_true",
        help="選定の上位候補（value/impact/risk/dependency 等）を標準出力に出す",
    )
    parser.add_argument(
        "--min-question-value",
        type=int,
        default=8,
        help="この値未満の候補は質問せず完了扱い（デフォルト: 8）",
    )
    parser.add_argument(
        "--question-model",
        default="gpt-4.1",
        help="質問生成で使うOpenAIモデル（デフォルト: gpt-4.1）",
    )
    args = parser.parse_args()

    job_dir = os.path.join(args.input_root, args.job_id)
    unknowns_path = args.unknowns or os.path.join(job_dir, "unknown_points.json")
    question_output = args.question_output or os.path.join(job_dir, "question_result.json")
    message_output = args.message_output or os.path.join(job_dir, "question_message.txt")
    os.makedirs(os.path.dirname(question_output) or ".", exist_ok=True)
    doc_url = _load_doc_url(job_dir)

    if not os.path.isfile(unknowns_path):
        raise FileNotFoundError(f"unknowns file not found: {unknowns_path}")
    unknown_points_all = load_unknown_points(unknowns_path)
    unknown_points, pending_meta = _filter_pending_unknown_points(unknown_points_all)

    context_text_path = resolve_context_text_path(args.job_id, args.input_root, args.text)
    full_text = ""
    if context_text_path:
        with open(context_text_path, "r", encoding="utf-8") as f:
            full_text = f.read()

    if not unknown_points:
        result_payload = {
            "job_id": args.job_id,
            "question_status": "none",
            "message": "未回答の不明箇所は0件のため、確認事項はありません。",
            "selected_unknown": None,
            "doc_url": doc_url,
            "selection_audit": {
                "selection_mode": "none_no_pending_unknowns",
                **pending_meta,
            },
            "question_text": "",
        }
    else:
        api_key, key_source = resolve_openai_api_key()
        print(f"question_generation_openai_api_key_found={bool(api_key)} source={key_source}")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set for AI question generation.")
        try:
            ai_result = _generate_one_question_by_ai(
                full_text=full_text,
                unknown_points=unknown_points,
                model=args.question_model,
                api_key=api_key,
            )
            question_status = str(ai_result.get("question_status", "generated")).strip() or "generated"
            selected_unknown = ai_result.get("selected_unknown")
            if not isinstance(selected_unknown, dict):
                selected_unknown = None
            question_text = _normalize_question_text(ai_result.get("question_text", ""))
            selection_audit = ai_result.get("selection_audit")
            if not isinstance(selection_audit, dict):
                selection_audit = {"selection_mode": "ai_global_context"}
            selection_audit["selection_mode"] = "ai_global_context"
            message = str(ai_result.get("message", "")).strip()

            if question_status == "none" or not question_text:
                result_payload = {
                    "job_id": args.job_id,
                    "question_status": "none",
                    "message": message or "全文文脈は提出可能水準のため、追加質問は行いません。",
                    "selected_unknown": selected_unknown,
                    "doc_url": doc_url,
                    "selection_audit": {**pending_meta, **selection_audit},
                    "question_text": "",
                }
            else:
                question_id = str(uuid.uuid4())
                result_payload = {
                    "job_id": args.job_id,
                    "question_id": question_id,
                    "question_status": "generated",
                    "message": "",
                    "selected_unknown": selected_unknown,
                    "doc_url": doc_url,
                    "selection_audit": {**pending_meta, **selection_audit},
                    "question_text": question_text,
                }
                write_line_pending_context(
                    job_id=args.job_id,
                    question_id=question_id,
                    question_text=question_text,
                    selected_unknown=(selected_unknown or {}),
                    selection_audit=selection_audit,
                )
        except Exception as e:
            print(f"question_generation_ai_failed={e!r}")
            # AI失敗時のみ既存ロジックへフォールバック
            if args.debug_selection:
                deduped_dbg, _ = deduplicate_unknown_points_by_type_text(unknown_points)
                print(format_top_candidates_debug(deduped_dbg, full_text), flush=True)
            selected, selection_audit = select_one_unknown_value_based(unknown_points, full_text)
            value = int(selection_audit.get("value", 0))
            if value < args.min_question_value:
                pop_value_fields(selected)
                result_payload = {
                    "job_id": args.job_id,
                    "question_status": "none",
                    "message": (
                        "推測に頼らず読める水準に達しているため、追加質問は行いません。"
                        f"（top_value={value}, threshold={args.min_question_value}）"
                    ),
                    "selected_unknown": selected,
                    "doc_url": doc_url,
                    "selection_audit": {**pending_meta, **selection_audit},
                    "question_text": "",
                }
            else:
                pop_value_fields(selected)
                question_text = _normalize_question_text(build_user_friendly_question(selected))
                question_id = str(uuid.uuid4())
                result_payload = {
                    "job_id": args.job_id,
                    "question_id": question_id,
                    "question_status": "generated",
                    "message": "",
                    "selected_unknown": selected,
                    "doc_url": doc_url,
                    "selection_audit": {**pending_meta, **selection_audit},
                    "question_text": question_text,
                }
                write_line_pending_context(
                    job_id=args.job_id,
                    question_id=question_id,
                    question_text=question_text,
                    selected_unknown=selected,
                    selection_audit=selection_audit,
                )

    with open(question_output, "w", encoding="utf-8") as f:
        json.dump(result_payload, f, ensure_ascii=False, indent=2)

    message_text = build_line_message(result_payload)
    with open(message_output, "w", encoding="utf-8") as f:
        f.write(message_text)

    line_push = "skipped"
    if args.send_line:
        line_user_id = os.getenv("LINE_USER_ID")
        line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        if not line_user_id:
            raise RuntimeError("LINE_USER_ID is not set.")
        if not line_token:
            raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN is not set.")
        push_line_message(
            channel_access_token=line_token,
            user_id=line_user_id,
            text=message_text,
        )
        if result_payload.get("question_status") == "generated":
            line_push = "sent_question"
        else:
            line_push = "sent_completion"

    print(f"job_id={args.job_id}")
    print(f"unknowns={unknowns_path}")
    print(f"context_text={context_text_path or '(not found)'}")
    print(f"question_result={question_output}")
    print(f"line_message={message_output}")
    print(f"question_status={result_payload.get('question_status')}")
    print(f"line_push={line_push}")


if __name__ == "__main__":
    main()
