import argparse
import json
import os
import uuid
from datetime import datetime, timezone

import requests

from ai_correct_text import resolve_openai_api_key
from generate_one_question import TYPE_PRIORITY, load_unknown_points
from meeting_profile import format_meeting_profile_for_prompt, load_meeting_profile
from knowledge_sheet_store import load_knowledge_memos
from line_send_question import build_line_message, push_line_message
from question_value_selection import (
    deduplicate_unknown_points_by_type_text,
    format_top_candidates_debug,
    pop_value_fields,
    select_one_unknown_value_based,
)
from repo_env import load_dotenv_local

LINE_PENDING_CONTEXT_PATH = os.path.join("data", "line_pending_context.json")
ASKED_QUESTIONS_FILENAME = "asked_questions.json"
QUESTION_SELECTION_AUDIT_FILENAME = "question_selection_audit.json"


def _asked_questions_path(job_dir: str) -> str:
    return os.path.join(job_dir, ASKED_QUESTIONS_FILENAME)


def _load_asked_questions(job_dir: str) -> list[dict]:
    """このジョブで既に LINE 送信した質問の履歴を読み込む。

    重複質問を抑制するため、AI への入力プロンプトと
    asked マーク判定の両方で参照する。
    """
    path = _asked_questions_path(job_dir)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _append_asked_question(
    job_dir: str,
    *,
    question_id: str,
    question_text: str,
    question_format: str,
    selected_unknown: dict | None,
) -> None:
    """質問送信時に履歴を追記する。"""
    history = _load_asked_questions(job_dir)
    history.append({
        "question_id": question_id,
        "question_text": question_text,
        "question_format": question_format,
        "selected_text": str((selected_unknown or {}).get("text") or "").strip(),
        "selected_type": str((selected_unknown or {}).get("type") or "").strip(),
        "selected_hypothesis": str((selected_unknown or {}).get("hypothesis") or "").strip(),
        "asked_at": datetime.now(timezone.utc).isoformat(),
    })
    path = _asked_questions_path(job_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _normalize_text_for_dedup(s: str) -> str:
    """重複判定用にテキストを正規化（空白・句読点・記号を圧縮）。"""
    if not s:
        return ""
    norm = s.strip().lower()
    for ch in [" ", "\t", "\u3000", "\n", "\r", "「", "」", "『", "』",
               "、", "。", ",", ".", "？", "?", "！", "!", "・", "-", "ー"]:
        norm = norm.replace(ch, "")
    return norm


def _is_similar_text(a: str, b: str, *, min_overlap: float = 0.7) -> bool:
    """正規化後のテキスト類似判定。

    - 短文（< 10文字）は完全一致と片方包含のみで判定（誤マッチ防止）
    - 中長文（>= 10文字）は完全一致 / 部分包含 / 2-gram の Jaccard 類似度で判定
    - 単独文字のオーバーラップ率は使わない（「さん」共通で誤マッチするため）
    """
    na = _normalize_text_for_dedup(a)
    nb = _normalize_text_for_dedup(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)

    # 短文同士は厳密判定のみ
    if len(short) < 10:
        # 短い方が完全に長い方に含まれる場合のみ類似扱い
        return len(short) >= 4 and short in long_

    # 中長文は部分包含なら類似
    if short in long_:
        return True

    # 2-gram の Jaccard 類似度
    def bigrams(s: str) -> set[str]:
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()

    ga = bigrams(na)
    gb = bigrams(nb)
    if not ga or not gb:
        return False
    inter = len(ga & gb)
    union = len(ga | gb)
    return union > 0 and (inter / union) >= min_overlap


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


def _extract_context_window(full_text: str, target_text: str, window: int = 200) -> str:
    """target_text の出現位置の前後 window 文字を切り出す。

    複数候補がある場合は最初の出現位置を採用。
    """
    if not full_text or not target_text:
        return ""
    needle = target_text.strip()[:50]  # 長すぎると find に失敗するので先頭50字で当たる
    if not needle:
        return ""
    pos = full_text.find(needle)
    if pos < 0:
        return ""
    start = max(0, pos - window)
    end = min(len(full_text), pos + len(needle) + window)
    excerpt = full_text[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(full_text) else ""
    return f"{prefix}{excerpt}{suffix}"


def _build_unknown_points_compact(
    unknown_points: list[dict],
    full_text: str = "",
    limit: int = 25,
) -> list[dict]:
    """検出結果を AI に渡す形に整形。前後文脈と hypothesis を必ず含める。"""
    out: list[dict] = []
    for item in unknown_points[:limit]:
        text_raw = str(item.get("text", "")).strip()
        entry = {
            "type": str(item.get("type", "")).strip(),
            "text": text_raw[:220],
            "reason": str(item.get("reason", "")).strip()[:220],
        }
        impact = item.get("proposal_impact")
        if impact is not None:
            try:
                entry["proposal_impact"] = int(impact)
            except (TypeError, ValueError):
                pass
        evidence = str(item.get("evidence", "")).strip()
        if evidence:
            entry["evidence"] = evidence[:200]
        hypothesis = str(item.get("hypothesis", "")).strip()
        if hypothesis:
            entry["hypothesis"] = hypothesis[:120]
        # 前後200字の文脈ウィンドウ
        ctx = _extract_context_window(full_text, text_raw, window=200)
        if ctx:
            entry["context_window"] = ctx[:600]
        out.append(entry)
    return out


def _format_knowledge_for_question_prompt(memos: list[str]) -> str:
    """質問生成プロンプト用のナレッジ整形。「これに該当するなら質問するな」を明示。"""
    if not memos:
        return ""
    lines = "\n".join(f"- {m}" for m in memos if m and m.strip())
    if not lines:
        return ""
    return (
        "\n\n【既知情報（質問しない対象・厳守）】\n"
        "以下はスプレッドシートに登録済みの用語・人名・組織です。\n"
        "これらに該当する不明点が候補に含まれていても、ユーザーへの質問対象から除外してください。\n"
        "（後段の補正で自動的に正しい表記へ修正されます）\n"
        f"{lines}"
    )


def _format_asked_questions_for_prompt(asked: list[dict]) -> str:
    """過去質問履歴を AI への入力プロンプト向けに整形。

    AI に「これと似た質問は二度と作るな」と強く指示するための材料。
    text と question_text の両方を渡し、論点重複を避けさせる。
    """
    if not asked:
        return ""
    lines: list[str] = []
    for q in asked[-15:]:  # 直近15件まで
        qt = str(q.get("question_text") or "").strip()
        st = str(q.get("selected_text") or "").strip()
        hyp = str(q.get("selected_hypothesis") or "").strip()
        if not qt and not st:
            continue
        bits: list[str] = []
        if qt:
            bits.append(f"質問:『{qt[:120]}』")
        if hyp:
            bits.append(f"仮説:『{hyp[:80]}』")
        if st:
            bits.append(f"該当:『{st[:80]}』")
        lines.append("- " + " / ".join(bits))
    if not lines:
        return ""
    return (
        "\n\n【既に同じジョブで送信済みの質問（重複禁止・厳守）】\n"
        "以下は今のジョブで既にユーザーへ送信した質問です。"
        "論点・該当箇所・仮説のいずれかが類似する質問を新たに作ってはいけません。"
        "もし候補がすべて既出と類似する場合は question_status='none' を返してください。\n"
        + "\n".join(lines)
    )


def _is_answered_unknown(item: dict) -> bool:
    status = str(item.get("status", "")).strip().lower()
    if status in {"answered", "done", "closed", "resolved"}:
        return True
    answer = item.get("answer")
    if isinstance(answer, str) and answer.strip():
        return True
    return False


def _is_asked_unknown(item: dict) -> bool:
    """質問を送信済み（回答待ち）の不明点かどうかを判定する。"""
    status = str(item.get("status", "")).strip().lower()
    return status == "asked"


def _filter_pending_unknown_points(unknown_points: list[dict]) -> tuple[list[dict], dict]:
    pending: list[dict] = []
    answered_count = 0
    asked_count = 0
    for item in unknown_points:
        if not isinstance(item, dict):
            continue
        if _is_answered_unknown(item):
            answered_count += 1
            continue
        if _is_asked_unknown(item):
            # 送信済みで回答待ちの質問は再送しない
            asked_count += 1
            continue
        pending.append(item)
    meta = {
        "unknown_points_count_before_filter": len(unknown_points),
        "answered_unknown_points_count": answered_count,
        "asked_unknown_points_count": asked_count,
        "pending_unknown_points_count": len(pending),
    }
    return pending, meta


def _is_coherence_review_point(item: dict) -> bool:
    return (
        str(item.get("type") or "") == "coherence_review"
        or str(item.get("source") or "") == "coherence_review"
    )


def _split_pending_by_source(pending: list[dict]) -> tuple[list[dict], list[dict]]:
    """既存(proposal_impact評価)対象と coherence_review FIFO副キューを分離。"""
    regular: list[dict] = []
    coherence: list[dict] = []
    for item in pending:
        if _is_coherence_review_point(item):
            coherence.append(item)
        else:
            regular.append(item)
    return regular, coherence


def _build_coherence_question_text(item: dict) -> str:
    """整合性レビュー由来の質問は候補語を提示せず、文脈+該当語+質問の3要素で組み立てる。"""
    context = str(item.get("text") or "").strip()
    if len(context) > 60:
        context = context[:60] + "…"
    word = str(item.get("anomaly_word") or "").strip()
    if not word:
        word = "該当箇所"
    lines = [
        "議事録の以下の箇所について確認させてください。",
        "",
        f"「{context}」",
        "",
        f"「{word}」が音声認識誤りの可能性があります。正しくは何でしょうか?",
    ]
    return "\n".join(lines)


def _make_coherence_question_payload(
    *,
    job_id: str,
    selected: dict,
    pending_meta: dict,
    doc_url: str,
) -> tuple[dict, str]:
    question_id = str(uuid.uuid4())
    question_text = _build_coherence_question_text(selected)
    selection_audit = {
        "selection_mode": "coherence_review_fifo",
        "anomaly_id": selected.get("anomaly_id"),
        "anomaly_type": selected.get("anomaly_type"),
        "confidence": selected.get("confidence"),
        "estimated_correction": selected.get("estimated_correction"),
        "question_format": "free_text",
        "proposal_impact": 0,
    }
    result_payload = {
        "job_id": job_id,
        "question_id": question_id,
        "question_status": "generated",
        "question_format": "free_text",
        "message": "",
        "selected_unknown": selected,
        "doc_url": doc_url,
        "selection_audit": {**pending_meta, **selection_audit},
        "question_text": question_text,
    }
    return result_payload, question_id


def _generate_one_question_by_ai(
    *,
    full_text: str,
    unknown_points: list[dict],
    model: str,
    api_key: str,
    timeout_sec: int = 180,
    knowledge_memos: list[str] | None = None,
    asked_questions: list[dict] | None = None,
    meeting_profile: dict | None = None,
) -> dict:
    """
    AI主導で「全体文脈に最も効く1問」を生成する。
    
    強化点:
    - 各 unknown_point に前後200字の context_window を含めて渡す
    - hypothesis があれば「○○で合っていますか？はい/いいえ で教えてください」形式に変換
    - ナレッジ既知の項目は質問対象から除外させる
    
    戻り値は dict:
      - question_status: generated / none
      - question_text
      - question_format: yes_no | free_text  ← 新規
      - selected_unknown
      - selection_audit
      - message
    """
    url = "https://api.openai.com/v1/responses"
    compact_unknowns = _build_unknown_points_compact(unknown_points, full_text=full_text)
    transcript = (full_text or "").strip()
    if len(transcript) > 12000:
        transcript = transcript[:12000]

    schema_hint = {
        "question_status": "generated|none",
        "question_text": "string（質問本文）",
        "question_format": "yes_no | free_text（hypothesisがあればyes_no優先）",
        "proposal_impact": "integer 1-10（選んだ不明点の proposal_impact）",
        "selected_unknown": {"type": "string", "text": "string", "reason": "string"},
        "selection_audit": {
            "selection_mode": "ai_global_context",
            "why_this_question": "string（なぜこの1問か。50字以内）",
            "resolved_if_answered": "string（回答で何が確定するか）",
            "confidence": "low|medium|high",
        },
        "message": "string",
    }
    user_payload = {
        "transcript_excerpt": transcript,
        "unknown_points": compact_unknowns,
        "output_schema": schema_hint,
    }
    knowledge_section = _format_knowledge_for_question_prompt(knowledge_memos or [])
    asked_section = _format_asked_questions_for_prompt(asked_questions or [])
    profile_section = format_meeting_profile_for_prompt(meeting_profile or {})
    system_prompt = (
        "あなたは議事録品質管理アシスタントです。"
        "目的は、全文の文脈が最も繋がるように、ユーザーへの質問を1問だけ選ぶことです。"
        + profile_section
        + "\n\n【1問の選び方】"
        "\n- 【最優先】proposal_impact が高いもの（次の打ち手への影響が大きい）"
        "\n- 音声認識の誤変換・意味不明語句（固有名詞/数値/文字化け）"
        "\n- 解釈確認（『この運用方針で合っていますか？』系）は原則出さない。"
        "文脈から読み取れる合意内容を疑う質問はユーザー負担が高く価値が低い"
        "\n- 細かな表記ゆれより、誤変換の修正に直結する論点を優先する"
        "\n- 質問の結果で本文全体がどこまで確定するかを重視する"
        "\n- 候補の context_window（前後文脈）を必ず読んで判断する"
        "\n- ユーザー側の回答コストが極端に高いものは避ける"
        "\n- 【既に送信済みの質問】と論点・該当箇所・仮説が類似するものは絶対に再質問しない"
        "\n\n【選定基準】"
        "\n- proposal_impact が高いものを優先（次の打ち手への影響が大きい）"
        "\n- 同じ impact なら、確認のしやすさ（Yes/Noで答えられる > 自由記述）を優先"
        "\n- 同じ impact かつ同じ形式なら、会議の前半で言及された論点を優先"
        "\n\n【質問文の形式】"
        "\n- 誤変換・固有名詞の確認は free_text を優先"
        "（例: 『〇〇』は『△△』の誤変換でしょうか？正しい表記を教えてください）"
        "\n- hypothesis（推測の正解）が候補に存在し、Yes/No で十分な場合のみ"
        "『○○で合っていますか？「はい」または「いいえ」で教えてください。』形式にし、"
        "question_format='yes_no' を返す"
        "\n- 解釈確認・運用方針の再確認のような低価値質問は question_status='none' を選ぶ"
        "\n- いずれの場合も、ユーザーが該当箇所を思い出しやすいよう、context_window から要点を1〜2文で含める"
        "\n- question_text は LINE 送信用の自然な日本語、原則120字以内（Yes/No質問は150字までOK）"
        "\n- question_text に job_id や question_id のような内部IDを書かない"
        "\n- selected_unknown.text の長文引用をそのまま貼らない"
        "\n\n【質問しない判断（question_status=none）】"
        "\n- 全候補が【既知情報】に該当する場合"
        "\n- 全候補が文脈から一意に解釈可能な場合"
        "\n- 議事録としてそのまま提出可能と判断できる場合"
        "\n\n出力は必ずJSONオブジェクトのみ。説明文を付けないでください。"
        + knowledge_section
        + asked_section
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


def _mark_unknown_point_asked(
    unknowns_path: str,
    selected_unknown: dict | None,
    question_id: str,
) -> int:
    """選定された不明点に status='asked' をマークする（同一質問の重複送信防止）。

    text 完全一致だけでなく、正規化後の類似一致と hypothesis 一致も判定材料にする。
    AI が同じ論点を別表現で選び直したケースでも asked マークが当たるようにする。
    """
    if not selected_unknown or not os.path.isfile(unknowns_path):
        return 0
    target_text = str(selected_unknown.get("text") or "").strip()
    target_type = str(selected_unknown.get("type") or "").strip()
    target_hypo = str(selected_unknown.get("hypothesis") or "").strip()
    target_anomaly_id = str(selected_unknown.get("anomaly_id") or "").strip()
    if not target_text and not target_hypo and not target_anomaly_id:
        return 0

    try:
        with open(unknowns_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        unknown_points = [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except Exception:
        return 0

    updated = 0
    for item in unknown_points:
        status = str(item.get("status", "")).strip().lower()
        if status in {"answered", "done", "closed", "resolved", "asked"}:
            continue
        item_text = str(item.get("text") or "").strip()
        item_type = str(item.get("type") or "").strip()
        item_hypo = str(item.get("hypothesis") or "").strip()
        item_anomaly_id = str(item.get("anomaly_id") or "").strip()
        # 型が違う候補は対象外（人名 vs 数値などを誤マークしない）
        if target_type and item_type and item_type != target_type:
            continue
        # マッチ条件: ⓪anomaly_id一致(整合性レビュー用)、①text完全一致、②text類似、③hypothesis一致
        match = False
        if target_anomaly_id and item_anomaly_id and item_anomaly_id == target_anomaly_id:
            match = True
        elif target_text and item_text and item_text == target_text:
            match = True
        elif target_text and item_text and _is_similar_text(item_text, target_text):
            match = True
        elif target_hypo and item_hypo and (
            item_hypo == target_hypo
            or _is_similar_text(item_hypo, target_hypo, min_overlap=0.7)
        ):
            match = True
        if not match:
            continue
        item["status"] = "asked"
        item["asked_by_question_id"] = question_id
        updated += 1

    if updated > 0:
        try:
            with open(unknowns_path, "w", encoding="utf-8") as f:
                json.dump(unknown_points, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return updated


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
        default=7,
        help="この proposal_impact 未満の候補は質問せず完了扱い（デフォルト: 7）",
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
    pending_all, pending_meta = _filter_pending_unknown_points(unknown_points_all)
    regular_pending, coherence_pending = _split_pending_by_source(pending_all)
    pending_meta["regular_pending_count"] = len(regular_pending)
    pending_meta["coherence_pending_count"] = len(coherence_pending)
    # 既存(proposal_impact)を優先。既存が空のときのみ整合性レビュー FIFO へフォールバック。
    unknown_points = regular_pending if regular_pending else []
    coherence_fallback_active = (not regular_pending) and bool(coherence_pending)

    context_text_path = resolve_context_text_path(args.job_id, args.input_root, args.text)
    full_text = ""
    if context_text_path:
        with open(context_text_path, "r", encoding="utf-8") as f:
            full_text = f.read()

    if coherence_fallback_active:
        selected = coherence_pending[0]
        result_payload, _qid = _make_coherence_question_payload(
            job_id=args.job_id,
            selected=selected,
            pending_meta=pending_meta,
            doc_url=doc_url,
        )
        write_line_pending_context(
            job_id=args.job_id,
            question_id=str(result_payload["question_id"]),
            question_text=str(result_payload["question_text"]),
            selected_unknown=selected,
            selection_audit=result_payload["selection_audit"],
        )
    elif not unknown_points:
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
        # Phase 2: Layer 2 由来の関連知識を渡す(空ならレガシー memos にフォールバック)。
        # 質問生成側は文字列ブロックではなく list を期待するため、
        # Layer 2 ブロックを 1要素の list として渡す。
        try:
            from world_knowledge_store import get_runtime_knowledge_block
            meeting_profile_tmp = load_meeting_profile(job_dir)
            world_block = get_runtime_knowledge_block(
                meeting_profile=meeting_profile_tmp, purpose="detection",
            )
            if world_block.strip():
                knowledge_memos = [world_block]
            else:
                knowledge_memos = load_knowledge_memos() or []
            print(f"question_generation_knowledge_chars={sum(len(m) for m in knowledge_memos)}")
        except Exception as e:
            knowledge_memos = []
            print(f"question_generation_knowledge_load_failed={e!r}")

        # 同一ジョブで既に送信済みの質問履歴を AI に渡して類似質問を抑制
        asked_history = _load_asked_questions(job_dir)
        print(f"question_generation_asked_history_count={len(asked_history)}")
        meeting_profile = load_meeting_profile(job_dir)

        try:
            ai_result = _generate_one_question_by_ai(
                full_text=full_text,
                unknown_points=unknown_points,
                model=args.question_model,
                api_key=api_key,
                knowledge_memos=knowledge_memos,
                asked_questions=asked_history,
                meeting_profile=meeting_profile,
            )
            question_status = str(ai_result.get("question_status", "generated")).strip() or "generated"
            question_format = str(ai_result.get("question_format", "")).strip().lower()
            if question_format not in {"yes_no", "free_text"}:
                question_format = "free_text"
            selected_unknown = ai_result.get("selected_unknown")
            if not isinstance(selected_unknown, dict):
                selected_unknown = None
            question_text = _normalize_question_text(ai_result.get("question_text", ""))
            try:
                selected_impact = int(ai_result.get("proposal_impact", 0))
            except (TypeError, ValueError):
                selected_impact = int((selected_unknown or {}).get("proposal_impact") or 0)
            selection_audit = ai_result.get("selection_audit")
            if not isinstance(selection_audit, dict):
                selection_audit = {"selection_mode": "ai_global_context"}
            selection_audit["selection_mode"] = "ai_global_context"
            selection_audit["question_format"] = question_format
            selection_audit["proposal_impact"] = selected_impact
            message = str(ai_result.get("message", "")).strip()

            if (
                question_status == "none"
                or not question_text
                or selected_impact < args.min_question_value
            ):
                result_payload = {
                    "job_id": args.job_id,
                    "question_status": "none",
                    "message": message or (
                        f"proposal_impact={selected_impact} が閾値 {args.min_question_value} 未満のため、"
                        "追加質問は行いません。"
                        if selected_impact < args.min_question_value and question_text
                        else "全文文脈は提出可能水準のため、追加質問は行いません。"
                    ),
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
                    "question_format": question_format,
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

    audit_path = os.path.join(job_dir, QUESTION_SELECTION_AUDIT_FILENAME)
    audit_payload = {
        "job_id": args.job_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question_status": result_payload.get("question_status"),
        "message": result_payload.get("message"),
        "question_text": result_payload.get("question_text"),
        "selected_unknown": result_payload.get("selected_unknown"),
        "selection_audit": result_payload.get("selection_audit"),
        "min_question_value": args.min_question_value,
        "unknown_points_pending_count": len(unknown_points),
        "context_text_path": context_text_path,
    }
    with open(audit_path, "w", encoding="utf-8") as f:
        json.dump(audit_payload, f, ensure_ascii=False, indent=2)

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
            # 送信済みとしてマーク（同一質問の重複送信防止）
            _mark_unknown_point_asked(
                unknowns_path=unknowns_path,
                selected_unknown=result_payload.get("selected_unknown"),
                question_id=str(result_payload.get("question_id") or ""),
            )
            # 質問履歴に追記（次サイクルで AI へ既出として渡し、類似質問を抑制する）
            _append_asked_question(
                job_dir=job_dir,
                question_id=str(result_payload.get("question_id") or ""),
                question_text=str(result_payload.get("question_text") or ""),
                question_format=str(result_payload.get("question_format") or ""),
                selected_unknown=result_payload.get("selected_unknown"),
            )
        else:
            line_push = "sent_completion"

    print(f"job_id={args.job_id}")
    print(f"unknowns={unknowns_path}")
    print(f"context_text={context_text_path or '(not found)'}")
    print(f"question_result={question_output}")
    print(f"question_selection_audit={audit_path}")
    print(f"line_message={message_output}")
    print(f"question_status={result_payload.get('question_status')}")
    print(f"line_push={line_push}")


if __name__ == "__main__":
    main()
