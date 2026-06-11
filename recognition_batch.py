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


# LINE で 1 語ずつ確認する際の anomaly_word 最小長（「味」等の熟語内部分一致を除外）
COHERENCE_MIN_QUESTION_WORD_LEN = 3

_BOUNDARY_CHARS = set(
    " \t\n\r。、.,!?！？…：:；;''\"()（）[]【】「」『』/／・｜|"
)
# 直後に付きやすい助詞（「味が」などは単語として独立とみなす）
_TRAILING_PARTICLES = set("のにはがをでもとやかねよわ")
# 直前がこれらの漢字/カナのとき、1〜2 字語は熟語の途中とみなす（意|味 など）
_COMPOUND_INTERIOR_PREV = _TRAILING_PARTICLES  # の|味 は OK、意|味 は NG


def is_valid_coherence_question_word(word: str) -> bool:
    w = str(word or "").strip()
    return len(w) >= COHERENCE_MIN_QUESTION_WORD_LEN


def _is_cjk_letter(ch: str) -> bool:
    if not ch:
        return False
    o = ord(ch)
    return (
        0x3040 <= o <= 0x30FF
        or 0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
    )


def _is_kanji(ch: str) -> bool:
    if not ch:
        return False
    o = ord(ch)
    return 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF


def _is_standalone_word_at(text: str, idx: int, length: int) -> bool:
    """text[idx:idx+length] が熟語の途中でない独立した語か。"""
    if idx < 0 or length <= 0 or idx + length > len(text):
        return False

    if idx == 0:
        left_ok = True
    else:
        prev = text[idx - 1]
        if prev in _BOUNDARY_CHARS:
            left_ok = True
        elif prev in _COMPOUND_INTERIOR_PREV:
            left_ok = True
        elif length <= 2 and _is_kanji(prev):
            # 意|味 のように直前が漢字なら熟語の途中
            left_ok = False
        elif length <= 2 and _is_cjk_letter(prev):
            # という|意味 のように直前がかななら独立語の可能性が高い
            left_ok = True
        else:
            left_ok = not _is_cjk_letter(prev)

    right_idx = idx + length
    if right_idx >= len(text):
        right_ok = True
    else:
        nxt = text[right_idx]
        if nxt in _BOUNDARY_CHARS:
            right_ok = True
        elif nxt in _TRAILING_PARTICLES:
            right_ok = True
        elif length <= 2 and _is_cjk_letter(nxt):
            right_ok = False
        else:
            right_ok = True

    return left_ok and right_ok


def find_standalone_word(text: str, word: str, hint_pos: int = -1) -> int:
    """word の独立出現位置を返す。hint_pos に最も近い候補を優先。"""
    w = str(word or "").strip()
    if not w or not text:
        return -1
    candidates: list[int] = []
    start = 0
    while start <= len(text):
        idx = text.find(w, start)
        if idx < 0:
            break
        if _is_standalone_word_at(text, idx, len(w)):
            candidates.append(idx)
        start = idx + 1 if idx >= start else start + 1
    if not candidates:
        return -1
    if isinstance(hint_pos, int) and hint_pos >= 0:
        return min(candidates, key=lambda i: abs(i - hint_pos))
    return candidates[0]


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
        if not word or not is_valid_coherence_question_word(word):
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
    action, correction = _normalize_answer_token(
        str(answer_text or "").strip(), target_word=word
    )
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
        if not word or word in seen_words or not is_valid_coherence_question_word(word):
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


def _normalize_answer_surface(s: str) -> str:
    t = str(s or "").strip().strip("「」『』\"' 　")
    t = re.sub(r"[。．.!！?？]+$", "", t)
    return t.strip()


_DELETE_PATTERNS = [
    re.compile(r"削除"),
    re.compile(r"消して"),
    re.compile(r"取り除"),
    re.compile(r"意味をなさ"),
    re.compile(r"意味がない"),
    re.compile(r"意味不明"),
    re.compile(r"ナンセンス"),
    re.compile(r"不要"),
    re.compile(r"聞き.?取り.?ミス"),
    re.compile(r"幻聴"),
    re.compile(r"存在しない"),
    re.compile(r"なかった"),
]


def _normalize_span_for_match(s: str) -> str:
    t = str(s or "")
    t = t.replace(VERIFY_TAG, "")
    t = re.sub(r"[【】「」『』\"'（）()]", "", t)
    t = re.sub(r"\s+", "", t)
    return t


def _is_delete_answer(s: str) -> bool:
    surface = str(s or "").strip()
    if not surface:
        return False
    return any(p.search(surface) for p in _DELETE_PATTERNS)


def _extract_delete_span_from_answer(s: str) -> str:
    quoted = [q.strip() for q in re.findall(r"「([^」]+)」", str(s or "")) if q.strip()]
    if quoted:
        return max(quoted, key=len)
    m = re.search(r"[「『\"]([^」』\"]{8,})[」』\"]", str(s or ""))
    if m:
        return m.group(1).strip()
    return ""


def _expand_to_delete_span(text: str, idx: int, length: int) -> tuple[int, int]:
    start = idx
    while start > 0 and text[start - 1] not in "。！？!?\n":
        start -= 1
    end = idx + length
    commas = 0
    while end < len(text):
        ch = text[end]
        if ch in "。！？!?\n":
            end += 1
            break
        if ch == "、":
            commas += 1
            end += 1
            if commas >= 2:
                break
            continue
        end += 1
    return start, end


def _expand_to_sentence_span(text: str, idx: int, length: int) -> tuple[int, int]:
    start = idx
    end = idx + length
    while start > 0 and text[start - 1] not in "。！？!?\n":
        start -= 1
    while end < len(text) and text[end] not in "。！？!?\n":
        end += 1
    if end < len(text) and text[end] in "。！？!?":
        end += 1
    return start, end


def _find_delete_span(transcript: str, *, span_hint: str, word: str) -> tuple[int, int] | None:
    w = str(word or "").strip()
    if not w:
        return None
    idx = find_standalone_word(transcript, w)
    tagged = f"{w}{VERIFY_TAG}"
    if idx < 0:
        idx = transcript.find(tagged)
    if idx < 0:
        idx = transcript.find(w)
    if idx < 0:
        return None
    if transcript[idx : idx + len(tagged)] == tagged:
        core_len = len(tagged)
    else:
        core_len = len(w)
    start = idx
    if span_hint and w in span_hint:
        hp = span_hint.find(w)
        raw_prefix = span_hint[max(0, hp - 24) : hp]
        norm_prefix = _normalize_span_for_match(raw_prefix)
        if len(norm_prefix) >= 8:
            tail = norm_prefix[-10:]
            for back in range(idx, max(-1, idx - 40), -1):
                seg = _normalize_span_for_match(transcript[back:idx])
                if not seg:
                    continue
                if seg.endswith(tail) or (len(seg) >= 6 and tail.endswith(seg)):
                    start = back
                    break
    length = (idx - start) + core_len
    return _expand_to_delete_span(transcript, start, length)


def _apply_delete_to_transcript(
    transcript: str, *, span_hint: str, word: str
) -> tuple[str, dict | None]:
    span = _find_delete_span(transcript, span_hint=span_hint, word=word)
    if span is None:
        return transcript, None
    start, end = span
    deleted = transcript[start:end]
    out = (transcript[:start] + transcript[end:]).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out, {
        "before": deleted.strip(),
        "after": "",
        "action": "delete",
        "word": word,
    }


def _looks_like_correction_answer(s: str, target_word: str = "") -> bool:
    if _is_delete_answer(s):
        return False
    if re.search(r"→|⇒|じゃなく|ではなく|じゃない|ではない|違う|誤り", s):
        return True
    quoted = re.findall(r"「([^」]+)」", s)
    tw = str(target_word or "").strip()
    for q in quoted:
        q = q.strip()
        if not q:
            continue
        if tw and q == tw:
            continue
        if q in {"OK", "ok", "不明"}:
            continue
        return True
    return False


_KEEP_EXACT = {
    "ok",
    "okです",
    "okです。",
    "okay",
    "そのまま",
    "そのままで",
    "そのままでいい",
    "そのままでいいです",
    "そのままで大丈夫",
    "そのままで問題ない",
    "合ってる",
    "合ってます",
    "合っています",
    "合っています。",
    "あってる",
    "あってます",
    "あっています",
    "問題ない",
    "問題なし",
    "問題ありません",
    "問題ありません。",
    "正しい",
    "正しいです",
    "正しいです。",
    "正しい表記",
    "正しい表記です",
    "正しい表記です。",
    "表記は正しい",
    "表記は正しいです",
    "表記は正しいです。",
    "そのとおり",
    "そのとおりです",
    "その通り",
    "その通りです",
    "はい",
    "はい。",
    "ええ",
    "うん",
    "大丈夫",
    "大丈夫です",
    "変更不要",
    "修正不要",
    "訂正不要",
    "間違いない",
    "間違いありません",
    "誤りではない",
    "誤変換ではない",
    "ミスではない",
    "このままで",
    "このままでいい",
    "このままで大丈夫",
    "間違いなく正しい",
    "認識は合っている",
    "認識合ってます",
}
_KEEP_PATTERNS = [
    re.compile(r"^ok[\.。!！]?$", re.I),
    re.compile(r"正し(い|かった)"),
    re.compile(r"正しい表記"),
    re.compile(r"表記.{0,6}正し"),
    re.compile(r"合ってい"),
    re.compile(r"あってい"),
    re.compile(r"問題(ない|なし|ありません)"),
    re.compile(r"その(通り|とおり)"),
    re.compile(r"変更不要|修正不要|訂正不要"),
    re.compile(r"誤り(ではない|じゃない)|誤変換ではない|ミスではない"),
    re.compile(r"間違い(ない|ありません|なく)"),
    re.compile(r"大丈夫"),
    re.compile(r"このまま(で)?(大丈夫|いい|よい|問題ない)?"),
    re.compile(r"そのまま(で)?(大丈夫|いい|よい|問題ない)?"),
    re.compile(r"^(はい|ええ|うん)([。.!！]?|、.*)?$"),
    re.compile(r"間違いなく"),
    re.compile(r"認識(は|も)?合っ"),
    re.compile(r"特に問題(ない|なし)"),
    re.compile(r"修正の必要(は|が)?ない"),
]


def _is_keep_answer(s: str, target_word: str = "") -> bool:
    surface = _normalize_answer_surface(s)
    if not surface:
        return False
    if _looks_like_correction_answer(surface, target_word):
        return False
    low = surface.lower().replace(" ", "")
    if low in _KEEP_EXACT:
        return True
    compact = low.replace("です", "").replace("ます", "").replace("。", "")
    if compact in {k.replace("です", "").replace("ます", "") for k in _KEEP_EXACT}:
        return True
    return any(p.search(surface) for p in _KEEP_PATTERNS)


def _normalize_answer_token(token: str, target_word: str = "") -> tuple[str, str]:
    """単一項目の回答トークンを (action, correction) に正規化する。

    action: "keep"(そのまま) / "unknown"(不明) / "correct"(訂正あり)
    """
    s = _normalize_answer_surface(token)
    if not s:
        return ("unknown", "")
    if _is_delete_answer(s):
        return ("delete", _extract_delete_span_from_answer(s))
    if _is_keep_answer(s, target_word):
        return ("keep", "")
    low = s.lower().replace(" ", "")
    unknown_words = {
        "不明",
        "わからない",
        "分からない",
        "わかりません",
        "分かりません",
        "覚えてない",
        "おぼえてない",
        "忘れた",
        "わすれた",
        "スキップ",
        "skip",
        "パス",
        "pass",
        "未定",
        "不明です",
    }
    if low in unknown_words or any(
        low.startswith(p) for p in ("わから", "分から", "不明")
    ):
        return ("unknown", "")
    # 「〇〇です」で引用があれば訂正語として抽出
    m = re.search(r"「([^」]+)」", s)
    if m:
        return ("correct", m.group(1).strip())
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
            action, correction = _normalize_answer_token(token, target_word=str(it.get("word") or ""))
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
        if action == "delete":
            before = out
            out, deleted = _apply_delete_to_transcript(
                out, span_hint=correction, word=word
            )
            if out != before and deleted:
                applied.append(
                    {
                        "anomaly_id": p.get("anomaly_id", ""),
                        "before": deleted.get("before", ""),
                        "after": "",
                        "action": "delete",
                        "word": word,
                    }
                )
            continue
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
