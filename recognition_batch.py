"""音声認識ゆれ([要確認] / coherence_review 由来)の回答解析・一括補正ユーティリティ。

設計意図:
- LINE は 1 質問 = 1 回答。認識ゆれも 1 語 1 問で確認する。
- 各回答では本文を書き換えず、unknown_points に記録するだけ。
- 未回答の認識ゆれが 0 件になった時点で、蓄積した回答を一括適用する。

レガシー: recognition_batch 形式(番号付き一括)への回答解析も残す。
"""
from __future__ import annotations

import json
import re

import requests

RECOGNITION_BATCH_FORMAT = "recognition_batch"
RECOGNITION_BATCH_TYPE = "recognition_batch"
VERIFY_TAG = "[要確認]"
COHERENCE_SOURCE = "coherence_review"
COHERENCE_TYPE = "coherence_review"


def is_coherence_unknown_item(item: dict | None) -> bool:
    if not isinstance(item, dict):
        return False
    return (
        str(item.get("source") or "") == COHERENCE_SOURCE
        or str(item.get("type") or "") == COHERENCE_TYPE
    )


# 1 通で確認する最大件数。長い逐語録では20件程度まで一括確認する。
MAX_BATCH_ITEMS = 20
# 各項目に添える前後文脈の最大文字数。
CONTEXT_PREVIEW_CHARS = 40

_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


def _clean_context(raw: str, anomaly_word: str) -> str:
    s = " ".join(str(raw or "").strip().split())
    if not s:
        return ""
    # context が anomaly_word そのものだけなら文脈として無意味なので落とす
    if s == anomaly_word:
        return ""
    if len(s) > CONTEXT_PREVIEW_CHARS:
        s = s[:CONTEXT_PREVIEW_CHARS].rstrip() + "…"
    return s


_CONF_RANK = {"medium": 0, "low": 1, "high": 2}


def select_next_coherence_point(coherence_points: list[dict]) -> dict | None:
    """未回答の coherence 項目から次に聞く 1 件を選ぶ(medium 優先・出現位置順)。"""
    ranked: list[tuple[tuple[int, int], dict]] = []
    for idx, p in enumerate(coherence_points):
        if not isinstance(p, dict):
            continue
        word = str(p.get("anomaly_word") or "").strip()
        if not word:
            continue
        conf = str(p.get("confidence") or "low").strip().lower()
        pos = p.get("context_position_in_transcript")
        try:
            pos_key = int(pos) if pos is not None else idx
        except (TypeError, ValueError):
            pos_key = idx
        ranked.append(((_CONF_RANK.get(conf, 9), pos_key), p))
    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1]


def parse_single_coherence_answer(answer_text: str, *, word: str) -> dict:
    """1 語 1 問への回答を (action, correction) に正規化する。"""
    action, correction = _normalize_answer_token(str(answer_text or "").strip())
    if action == "correct":
        if not correction:
            action = "unknown"
        elif correction == word:
            action = "keep"
    return {
        "word": word,
        "action": action,
        "correction": correction if action == "correct" else "",
    }


def build_batch_items(coherence_points: list[dict], *, limit: int = MAX_BATCH_ITEMS) -> list[dict]:
    """unknown_points の coherence 由来項目から、バッチ確認用 items を作る。

    anomaly_word が空のもの・重複(同一 word)は除外する。
    medium を優先し、同順位は transcript 上の出現位置順。
    """
    ranked: list[tuple[tuple[int, int], dict]] = []
    seen_words: set[str] = set()
    for idx, p in enumerate(coherence_points):
        word = str(p.get("anomaly_word") or "").strip()
        if not word or word in seen_words:
            continue
        seen_words.add(word)
        conf = str(p.get("confidence") or "low").strip().lower()
        pos = p.get("context_position_in_transcript")
        try:
            pos_key = int(pos) if pos is not None else idx
        except (TypeError, ValueError):
            pos_key = idx
        ranked.append(
            (
                (_CONF_RANK.get(conf, 9), pos_key),
                {
                    "anomaly_id": str(p.get("anomaly_id") or "").strip(),
                    "word": word,
                    "context": _clean_context(str(p.get("text") or ""), word),
                    "estimated_correction": str(p.get("estimated_correction") or "").strip(),
                    "anomaly_type": str(p.get("anomaly_type") or "").strip(),
                },
            )
        )
    ranked.sort(key=lambda x: x[0])
    items = [item for _, item in ranked[:limit]]
    return items


def build_batch_question_text(items: list[dict]) -> str:
    """番号付きの一括確認メッセージ本文を組み立てる。"""
    lines = [
        "議事録の音声認識で表記が不確かな箇所をまとめて確認させてください。",
        "番号ごとに正しい表記を教えてください。",
        "（合っていれば「OK」、分からなければ「不明」で構いません）",
        "",
    ]
    for i, it in enumerate(items, 1):
        ctx = it.get("context") or ""
        ctx_part = f"（…{ctx}…）" if ctx else ""
        lines.append(f"{i}.「{it['word']}」{ctx_part}")
    lines.append("")
    lines.append("例) 1 ドライマンゴー / 2 OK / 3 6ピース診断 / 4 不明")
    return "\n".join(lines)


def _normalize_answer_token(token: str) -> tuple[str, str]:
    """単一項目の回答トークンを (action, correction) に正規化する。

    action: "keep"(そのまま) / "unknown"(不明) / "correct"(訂正あり)
    """
    s = str(token or "").strip().strip("「」『』\"' 　")
    if not s:
        return ("unknown", "")
    low = s.lower().replace(" ", "")
    keep_words = {"ok", "okです", "okです。", "そのまま", "そのままで", "合ってる",
                  "合ってます", "あってる", "あってます", "問題ない", "問題なし",
                  "正しい", "正しいです", "そのとおり", "その通り", "はい"}
    unknown_words = {"不明", "わからない", "分からない", "わかりません", "分かりません",
                     "覚えてない", "おぼえてない", "忘れた", "わすれた", "スキップ", "skip"}
    if low in keep_words:
        return ("keep", "")
    if low in unknown_words:
        return ("unknown", "")
    return ("correct", s)


def _parse_numbered_answer_with_regex(answer_text: str, items: list[dict]) -> list[dict] | None:
    """「1 ○○ / 2 OK」のような番号付き回答を素朴にパースする。

    番号が 1 件も拾えなければ None を返し、LLM 解析にフォールバックさせる。
    """
    text = str(answer_text or "").strip()
    if not text:
        return None
    # 区切り(改行 / スラッシュ / 全角スラッシュ / 読点)を跨いで「番号 + 値」を拾う
    # 例: "1 ドライマンゴー", "2.OK", "③ 6ピース診断"
    circled = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    matches: dict[int, str] = {}
    pattern = re.compile(
        r"(?:^|[\n/／、,])\s*"
        r"(?:(\d{1,2})|([" + circled + r"]))"
        r"\s*[\.\):：\.、]?\s*"
        r"([^\n/／]*)"
    )
    for m in pattern.finditer("\n" + text):
        if m.group(1):
            idx = int(m.group(1))
        else:
            idx = circled.index(m.group(2)) + 1
        val = (m.group(3) or "").strip()
        if 1 <= idx <= len(items):
            matches[idx] = val
    if not matches:
        return None
    out: list[dict] = []
    for i, it in enumerate(items, 1):
        token = matches.get(i)
        if token is None:
            # 言及されなかった項目は「不明(未回答)」扱い
            action, correction = ("unknown", "")
        else:
            action, correction = _normalize_answer_token(token)
        out.append(
            {
                "anomaly_id": it.get("anomaly_id", ""),
                "word": it["word"],
                "action": action,
                "correction": correction,
            }
        )
    return out


def _extract_output_text(result: dict) -> str:
    texts: list[str] = []
    for item in result.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                texts.append(str(content.get("text") or ""))
    return "\n".join(t for t in texts if t).strip()


def _parse_batch_answer_with_llm(
    *,
    answer_text: str,
    items: list[dict],
    api_key: str,
    model: str,
    timeout_sec: int,
) -> list[dict]:
    """LLM で回答テキストを各項目の (action, correction) にマッピングする。"""
    item_payload = [
        {"index": i + 1, "word": it["word"], "context": it.get("context", "")}
        for i, it in enumerate(items)
    ]
    system_prompt = (
        "あなたは議事録の表記確認アシスタントです。"
        "音声認識で不確かだった語のリスト(items)と、ユーザーの一括回答(answer_text)が与えられます。"
        "各 item について、ユーザーが正しい表記を指定したか・そのままで良いと言ったか・不明としたかを判定してください。"
        "\n出力は JSON 配列のみ。各要素は次のキーを持つ:"
        '\n{"index": 整数(itemsのindex),'
        ' "action": "correct"(訂正あり) | "keep"(そのままで良い/OK) | "unknown"(不明・未回答),'
        ' "correction": "actionがcorrectのときの正しい表記。それ以外は空文字"}'
        "\n判定ルール:"
        "\n- ユーザーが具体的な語を書いていれば correct とし、その語を correction に入れる。"
        "\n- 『OK』『そのまま』『合ってる』等は keep。"
        "\n- 『不明』『わからない』や、その番号に言及が無い場合は unknown。"
        "\n- 番号と語の対応はユーザー回答の番号付けを尊重する。"
    )
    user_payload = {"items": item_payload, "answer_text": answer_text}
    resp = requests.post(
        _OPENAI_RESPONSES_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0.0,
            "max_output_tokens": 1500,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        },
        timeout=timeout_sec,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI API error: status={resp.status_code} body={resp.text[:500]}")
    content = _extract_output_text(resp.json())
    if not content:
        raise RuntimeError("OpenAI response did not contain output_text.")
    s = content.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        s = s[nl + 1:] if nl >= 0 else s
        if s.endswith("```"):
            s = s[:-3].strip()
    parsed = json.loads(s)
    if not isinstance(parsed, list):
        raise RuntimeError("LLM batch answer output is not an array.")
    by_index: dict[int, dict] = {}
    for el in parsed:
        if not isinstance(el, dict):
            continue
        try:
            idx = int(el.get("index"))
        except (TypeError, ValueError):
            continue
        by_index[idx] = el
    out: list[dict] = []
    for i, it in enumerate(items, 1):
        el = by_index.get(i, {})
        action = str(el.get("action") or "unknown").strip().lower()
        if action not in {"correct", "keep", "unknown"}:
            action = "unknown"
        correction = str(el.get("correction") or "").strip().strip("「」『』\"'")
        if action == "correct" and not correction:
            action = "unknown"
        out.append(
            {
                "anomaly_id": it.get("anomaly_id", ""),
                "word": it["word"],
                "action": action,
                "correction": correction,
            }
        )
    return out


def parse_batch_answer(
    *,
    answer_text: str,
    items: list[dict],
    api_key: str | None = None,
    model: str = "gpt-4.1",
    timeout_sec: int = 120,
) -> list[dict]:
    """回答テキストを各 item の補正指示に展開する。

    まず番号付き回答を正規表現で素朴に解析し、取れなければ(または曖昧なら)
    LLM 解析にフォールバックする。LLM が使えない/失敗した場合は regex 結果、
    それも無ければ全件 unknown を返す(=本文は変更しない安全側)。
    """
    if not items:
        return []

    regex_parsed = _parse_numbered_answer_with_regex(answer_text, items)

    # 番号がほぼ全項目ぶん取れていれば regex を採用(API 不要で確実)。
    if regex_parsed is not None:
        answered = sum(1 for p in regex_parsed if p["action"] != "unknown")
        if answered >= max(1, len(items) // 2):
            return regex_parsed

    if api_key:
        try:
            return _parse_batch_answer_with_llm(
                answer_text=answer_text,
                items=items,
                api_key=api_key,
                model=model,
                timeout_sec=timeout_sec,
            )
        except Exception as e:  # noqa: BLE001
            print(f"recognition_batch_llm_parse_failed={e!r}")

    if regex_parsed is not None:
        return regex_parsed
    return [
        {"anomaly_id": it.get("anomaly_id", ""), "word": it["word"], "action": "unknown", "correction": ""}
        for it in items
    ]


def apply_batch_corrections(transcript: str, parsed: list[dict]) -> tuple[str, list[dict]]:
    """parsed の指示に従って本文を一括補正し、[要確認] タグを処理する。

    - correct: word(+[要確認]) を correction に置換(全出現)。
    - keep:    word の直後の [要確認] タグだけ除去(語自体は確定でそのまま)。
    - unknown: 何もしない([要確認] は残す)。

    返り値: (補正後テキスト, applied)。applied は実際に変更した項目の記録。
    """
    out = transcript
    applied: list[dict] = []
    for p in parsed:
        word = str(p.get("word") or "").strip()
        if not word:
            continue
        action = str(p.get("action") or "unknown").strip().lower()
        correction = str(p.get("correction") or "").strip()
        # 同じ語が返ってきた(=表記は正しい)場合は keep と同じく確定扱い(タグ除去)。
        if action == "correct" and (not correction or correction == word):
            action = "keep"
        if action == "correct":
            tagged = f"{word}{VERIFY_TAG}"
            before = out
            if tagged in out:
                out = out.replace(tagged, correction)
            if word in out:
                out = out.replace(word, correction)
            if out != before:
                applied.append(
                    {"anomaly_id": p.get("anomaly_id", ""), "before": word,
                     "after": correction, "action": "correct"}
                )
        elif action == "keep":
            tagged = f"{word}{VERIFY_TAG}"
            if tagged in out:
                out = out.replace(tagged, word)
                applied.append(
                    {"anomaly_id": p.get("anomaly_id", ""), "before": tagged,
                     "after": word, "action": "keep"}
                )
        # unknown: 変更しない
    return out, applied
