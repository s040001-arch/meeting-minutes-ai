"""音声認識ゆれ([要確認] / coherence_review 由来)の回答解析・反映ユーティリティ。

設計意図 (Phase 4):
- LINE は 1 質問 = 1 回答。認識ゆれも 1 語 1 問で確認する。
- 各回答は merged_transcript_after_qa.txt に都度反映する（line_answer_reflect）。
- レガシー: recognition_batch 形式(番号付き一括)への回答解析も残す。
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

# span_hypothesis の仮説文から除くフィラー（意味は変えず口語のノイズのみ）
_HYPOTHESIS_FILLER_PHRASES: tuple[str, ...] = (
    "うんとかま",
    "お疲れ様です",
    "えーと",
    "ええと",
    "えっと",
    "そのー",
    "うーん",
    "なんか",
    "まあ",
    "あのね",
    "あの",
    "えー",
    "あー",
    "ええ",
    "うん",
)


def _remove_filler_phrase_at_boundaries(text: str, filler: str) -> str:
    """文頭・句読点直後など、フィラーとして独立している箇所だけ除去。"""
    if not filler:
        return text
    esc = re.escape(filler)
    text = re.sub(rf"(?m)^[ \t\u3000]*{esc}[ \t\u3000]*(?:[、，])?", "", text)
    text = re.sub(rf"(?<=[。！？!?])\s*{esc}[ \t\u3000]*(?:[、，])?", "", text)
    text = re.sub(rf"(?<=[、，])\s*{esc}[ \t\u3000]*(?:[、，])?", "", text)
    return text


def sanitize_hypothesis_fillers(text: str) -> str:
    """span_hypothesis の復元仮説から口語フィラーを除去する。

    ユーザーが OK だけで済むよう、仮説提示・反映の両方で使う。
    「はい」は相槌として残す（mechanical 補正と同様）。
    """
    s = str(text or "")
    if not s.strip():
        return s
    for filler in sorted(_HYPOTHESIS_FILLER_PHRASES, key=len, reverse=True):
        s = _remove_filler_phrase_at_boundaries(s, filler)
    # 語・助詞直後に続くソフトフィラー（「別になんか」「式であの」等）
    _after_filler = r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]"
    s = re.sub(
        rf"(?<=[\u4e00-\u9fff\u3040-\u309f])なんか(?={_after_filler})",
        "",
        s,
    )
    s = re.sub(
        rf"(?<=[\u4e00-\u9fff\u3040-\u309f])あの(?={_after_filler})",
        "",
        s,
    )
    # 文頭・句点直後・助詞直後の「ま」（まず/また/まだ は残す）
    s = re.sub(
        r"^ま(?!ず|た|だ|で|す|せ)(?=[\u4e00-\u9fff\u30a0-\u30ff])",
        "",
        s,
        flags=re.MULTILINE,
    )
    s = re.sub(
        r"(?<=[。！？!?、\nにをはがでと])ま(?!ず|た|だ|で|す|せ)(?=[\u4e00-\u9fff\u30a0-\u30ff])",
        "",
        s,
    )
    # 句末の相槌
    s = re.sub(r"(?:、)?(?:うん|ね|ええ)(?=[。!?！？?]|$)", "", s)
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"、{2,}", "、", s)
    s = re.sub(r"^[、，\s]+", "", s)
    s = re.sub(r"[、，\s]+(?=[。!?！？?])", "", s)
    return s.strip()


def is_coherence_unknown_item(item: dict | None) -> bool:
    if not isinstance(item, dict):
        return False
    return (
        str(item.get("source") or "") == COHERENCE_SOURCE
        or str(item.get("type") or "") == COHERENCE_TYPE
    )


# LINE で 1 語ずつ確認する際の anomaly_word 最小長（「味」等の熟語内部分一致を除外）
COHERENCE_MIN_QUESTION_WORD_LEN = 3
# 修正候補あり かつ 逐語録上で位置特定済みなら、2 字誤変換(決裁/同期/感覚 等)も許可
COHERENCE_MIN_QUESTION_WORD_LEN_WITH_CANDIDATE = 2

_BOUNDARY_CHARS = set(
    " \t\n\r。、.,!?！？…：:；;''\"()（）[]【】「」『』/／・｜|"
)
# 直後に付きやすい助詞（「味が」などは単語として独立とみなす）
_TRAILING_PARTICLES = set("のにはがをでもとやかねよわ")
# 直前がこれらの漢字/カナのとき、1〜2 字語は熟語の途中とみなす（意|味 など）
_COMPOUND_INTERIOR_PREV = _TRAILING_PARTICLES  # の|味 は OK、意|味 は NG


def is_valid_coherence_question_word(
    word: str, *, has_candidate: bool = False, located: bool = False
) -> bool:
    """LINE で確認する anomaly_word の妥当性判定。

    通常は 3 字以上を要求する（部分一致の誤検出爆発を防ぐ）。
    ただし「修正候補あり かつ 逐語録上で位置特定済み」の場合は 2 字まで許可し、
    日本語で頻出する 2 字誤変換（決裁/同期/感覚 等）を拾えるようにする。
    候補なし・位置不明の 2 字は従来どおり除外する。
    """
    w = str(word or "").strip()
    if not w:
        return False
    if (
        has_candidate
        and located
        and len(w) >= COHERENCE_MIN_QUESTION_WORD_LEN_WITH_CANDIDATE
    ):
        return True
    return len(w) >= COHERENCE_MIN_QUESTION_WORD_LEN


def _point_located(pos) -> bool:
    """context_position_in_transcript が有効な位置(>=0)かどうか。"""
    try:
        return pos is not None and int(pos) >= 0
    except (TypeError, ValueError):
        return False


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
# 各項目に添える前後文脈の最大文字数（短すぎるとどこを聞いているか分からない）。
CONTEXT_PREVIEW_CHARS = 120
# LINE 1通に収めるため、1項目あたりの表示上限。
BATCH_ITEM_DISPLAY_MAX = 160

_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


def _clean_context(raw: str, anomaly_word: str, *, max_chars: int = CONTEXT_PREVIEW_CHARS) -> str:
    s = " ".join(str(raw or "").strip().split())
    if not s:
        return ""
    # context が anomaly_word そのものだけなら文脈として無意味なので落とす
    if s == anomaly_word:
        return ""
    if len(s) > max_chars:
        # 該当語を中央付近に残す
        word = str(anomaly_word or "").strip()
        idx = s.find(word) if word else -1
        if idx >= 0:
            half = max(20, (max_chars - len(word)) // 2)
            start = max(0, idx - half)
            end = min(len(s), start + max_chars)
            if end - start < max_chars:
                start = max(0, end - max_chars)
            s = ("…" if start > 0 else "") + s[start:end] + ("…" if end < len(s) else "")
        else:
            s = s[:max_chars].rstrip() + "…"
    return s


def _highlight_word(display: str, word: str) -> str:
    """該当語を【】で囲み、どこを聞いているか一目で分かるようにする。"""
    w = str(word or "").strip()
    d = str(display or "").strip()
    if not w or not d or "【" in d:
        return d
    if w in d:
        return d.replace(w, f"【{w}】", 1)
    return d


def _format_batch_word_item_lines(
    *,
    index: int,
    loc_part: str,
    word: str,
    display: str,
    candidate: str,
    detected: str,
) -> list[str]:
    """単語確認バッチ1件分の表示行。該当語が引用内に無いときは明示する。"""
    w = str(word or "").strip()
    d = str(display or w).strip()
    highlighted = _highlight_word(d, w)
    if not highlighted and w:
        highlighted = w
    detected = str(detected or "").strip()
    note = f" ※検出時は「{detected}」" if detected and detected != w else ""
    cand = str(candidate or "").strip()
    if "【" not in highlighted and w:
        lines = [f"{index}.{loc_part}該当語「{w}」"]
        if d and d != w:
            lines.append(f"　前後文:「{d}」")
        if cand:
            lines.append(f"　→「{cand}」？{note}")
        else:
            lines.append(f"　→ 正しい語 / 削除 / 不明{note}")
        return lines
    if cand:
        return [f"{index}.{loc_part}「{highlighted}」→「{cand}」？{note}"]
    return [f"{index}.{loc_part}「{highlighted}」（正しい語 / 削除 / 不明）{note}"]


def _location_label(pos: int, total: int) -> str:
    if total <= 0 or pos < 0:
        return ""
    ratio = pos / total
    if ratio < 0.33:
        return "前半"
    if ratio < 0.66:
        return "中盤"
    return "後半"


def _char_overlap(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    # 同一長の並びで一致率（リング不足 vs リンク不足 を拾う）
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    same = sum(1 for i in range(n) if a[i] == b[i])
    return same / max(len(a), len(b))


def _best_fuzzy_in_window(window: str, word: str) -> tuple[str, int] | None:
    """window 内で word に最も近い同長〜近い長さの部分文字列を返す。"""
    w = str(word or "").strip()
    if not w or not window:
        return None
    best: tuple[float, str, int] | None = None
    for length in range(max(2, len(w) - 1), len(w) + 2):
        if length > len(window):
            continue
        for i in range(0, len(window) - length + 1):
            cand = window[i : i + length]
            if " " in cand or "\n" in cand:
                continue
            score = _char_overlap(cand, w)
            if score < 0.6:
                continue
            if best is None or score > best[0]:
                best = (score, cand, i)
    if best is None:
        return None
    return best[1], best[2]


def locate_surface_in_transcript(
    full_text: str,
    word: str,
    *,
    pos_hint: int = -1,
) -> tuple[str, int]:
    """現在の逐語録上の表記と位置を返す。

    検出時の anomaly_word が既に別表記へ変わっている場合（リング不足→リンク不足）でも、
    位置ヒント周辺の近似一致で「今ある文字列」を拾う。
    """
    w = str(word or "").strip()
    if not full_text or not w:
        return w, -1
    if pos_hint >= 0 and full_text[pos_hint : pos_hint + len(w)] == w:
        return w, pos_hint
    idx = find_standalone_word(full_text, w, hint_pos=pos_hint)
    if idx >= 0:
        return w, idx
    idx = full_text.find(w)
    if idx >= 0:
        return w, idx
    if pos_hint >= 0:
        win_start = max(0, pos_hint - 50)
        win_end = min(len(full_text), pos_hint + len(w) + 50)
        window = full_text[win_start:win_end]
        fuzzy = _best_fuzzy_in_window(window, w)
        if fuzzy:
            surface, local = fuzzy
            return surface, win_start + local
    # 全文から緩く探す（短い語の誤爆を避けるため長さ4以上）
    if len(w) >= 4:
        step = max(1, len(w) // 2)
        for i in range(0, max(1, len(full_text) - len(w) + 1), step):
            cand = full_text[i : i + len(w)]
            if _char_overlap(cand, w) >= 0.75:
                return cand, i
    return w, -1


def _snippet_at(full_text: str, idx: int, word: str) -> str:
    half = BATCH_ITEM_DISPLAY_MAX // 2
    start = max(0, idx - half)
    end = min(len(full_text), idx + len(word) + half)
    snippet = " ".join(full_text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(full_text) else ""
    return _clean_context(
        f"{prefix}{snippet}{suffix}", word, max_chars=BATCH_ITEM_DISPLAY_MAX
    )


def _display_snippet_for_point(point: dict, *, full_text: str = "") -> dict[str, str | int]:
    """現在の逐語録を優先して、表示用スニペットと surface_word を返す。

    古い span_text（検出時点の文言）は、逐語録に語が無いときだけフォールバックする。
    """
    word = str(point.get("anomaly_word") or point.get("word") or "").strip()
    pos_raw = point.get("context_position_in_transcript", -1)
    try:
        pos_hint = int(pos_raw)
    except (TypeError, ValueError):
        pos_hint = -1

    if full_text:
        surface, idx = locate_surface_in_transcript(
            full_text, word, pos_hint=pos_hint
        )
        if idx >= 0:
            display = _snippet_at(full_text, idx, surface)
            return {
                "display": _highlight_word(display, surface),
                "surface_word": surface,
                "position": idx,
                "location": _location_label(idx, len(full_text)),
                "found_in_transcript": True,
            }
        # 語は消えているが位置ヒントがある → 今そこにある文を見せる
        if pos_hint >= 0:
            display = _snippet_at(full_text, pos_hint, word)
            surface_at_hint, _ = locate_surface_in_transcript(
                full_text, word, pos_hint=pos_hint
            )
            return {
                "display": _highlight_word(display, surface_at_hint or word),
                "surface_word": surface_at_hint or word,
                "position": pos_hint,
                "location": _location_label(pos_hint, len(full_text)),
                "found_in_transcript": bool(surface_at_hint and surface_at_hint in full_text),
            }

    # full_text が無いときだけ検出時 span に頼る（陳腐化しやすい）
    span = str(point.get("span_text") or "").strip()
    if span:
        display = _clean_context(span, word, max_chars=BATCH_ITEM_DISPLAY_MAX)
        return {
            "display": _highlight_word(display, word),
            "surface_word": word,
            "position": pos_hint,
            "location": "",
            "found_in_transcript": False,
        }
    for key in ("context", "text"):
        cleaned = _clean_context(str(point.get(key) or ""), word, max_chars=BATCH_ITEM_DISPLAY_MAX)
        if cleaned:
            return {
                "display": _highlight_word(cleaned, word),
                "surface_word": word,
                "position": pos_hint,
                "location": "",
                "found_in_transcript": False,
            }
    return {
        "display": word,
        "surface_word": word,
        "position": pos_hint,
        "location": "",
        "found_in_transcript": False,
    }


_CONF_RANK = {"medium": 0, "low": 1, "high": 2}


def select_next_coherence_point(coherence_points: list[dict]) -> dict | None:
    """未回答の coherence 項目から次に聞く 1 件を選ぶ(medium 優先・出現位置順)。"""
    ranked: list[tuple[tuple[int, int], dict]] = []
    for idx, p in enumerate(coherence_points):
        if not isinstance(p, dict):
            continue
        word = str(p.get("anomaly_word") or "").strip()
        conf = str(p.get("confidence") or "low").strip().lower()
        pos = p.get("context_position_in_transcript")
        try:
            pos_key = int(pos) if pos is not None else idx
        except (TypeError, ValueError):
            pos_key = idx
        candidate = str(
            p.get("estimated_correction") or p.get("span_corrected") or ""
        ).strip()
        if not word or not is_valid_coherence_question_word(
            word, has_candidate=bool(candidate), located=_point_located(pos)
        ):
            continue
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


def build_batch_items(
    coherence_points: list[dict],
    *,
    limit: int = MAX_BATCH_ITEMS,
    full_text: str = "",
) -> list[dict]:
    """unknown_points の coherence 由来項目から、バッチ確認用 items を作る。

    anomaly_word が空のもの・重複(同一 word)は除外する。
    medium を優先し、同順位は transcript 上の出現位置順。
    span_text / 逐語録抜粋を使い、どこを聞いているか分かる文脈を載せる。
    """
    ranked: list[tuple[tuple[int, int], dict]] = []
    seen_words: set[str] = set()
    for idx, p in enumerate(coherence_points):
        word = str(p.get("anomaly_word") or "").strip()
        if not word or word in seen_words:
            continue
        conf = str(p.get("confidence") or "low").strip().lower()
        pos = p.get("context_position_in_transcript")
        try:
            pos_key = int(pos) if pos is not None else idx
        except (TypeError, ValueError):
            pos_key = idx

        # 文意確認モード: 崩壊した文スパンを引用し、復元仮説ごと確認する。
        # (a) 手動注入 question_kind=span_hypothesis、または
        # (b) 検出カテゴリC（文崩壊）で span_corrected の仮説があるもの。
        span_text = str(p.get("span_text") or "").strip()
        span_corr = str(p.get("span_corrected") or "").strip()
        explicit_span = str(p.get("question_kind") or "").strip() == "span_hypothesis"
        auto_span = (
            str(p.get("anomaly_type") or "").strip().upper() == "C"
            and len(span_text) >= 12
            and bool(span_corr)
            and span_corr != span_text
        )
        if (explicit_span or auto_span) and full_text and span_text:
            pos_span = full_text.find(span_text)
            if pos_span >= 0:
                if span_text in seen_words:
                    continue
                seen_words.add(span_text)
                seen_words.add(word)
                span_corr_clean = sanitize_hypothesis_fillers(span_corr)
                ranked.append(
                    (
                        (_CONF_RANK.get(conf, 9), pos_span),
                        {
                            "anomaly_id": str(p.get("anomaly_id") or "").strip(),
                            "word": span_text,
                            "detected_word": span_text,
                            "context": span_text,
                            "display": span_text[:220],
                            "estimated_correction": span_corr_clean,
                            "question_kind": "span_hypothesis",
                            "anomaly_type": "C",
                            "reason": str(p.get("reason") or "").strip()[:80],
                            "location": _location_label(pos_span, len(full_text)),
                            "position": pos_span,
                            "found_in_transcript": True,
                        },
                    )
                )
                continue
            # span が現逐語録に無い（陳腐化）→ 従来の word モードにフォールバック

        candidate = str(p.get("estimated_correction") or "").strip()
        if not candidate:
            span_corr = str(p.get("span_corrected") or "").strip()
            if span_corr and len(span_corr) <= 40 and "。" not in span_corr:
                candidate = span_corr
        resolved = _display_snippet_for_point(p, full_text=full_text)
        located = _point_located(pos) or bool(resolved.get("found_in_transcript"))
        # force_question は手動キュレーション済み項目のみ付与される免除フラグ
        if not bool(p.get("force_question")) and not is_valid_coherence_question_word(
            word, has_candidate=bool(candidate), located=located
        ):
            continue
        seen_words.add(word)
        surface = str(resolved.get("surface_word") or word).strip() or word
        # 既に候補どおりなら聞く必要なし
        if candidate and surface == candidate:
            continue
        display = str(resolved.get("display") or surface)
        ranked.append(
            (
                (_CONF_RANK.get(conf, 9), pos_key),
                {
                    "anomaly_id": str(p.get("anomaly_id") or "").strip(),
                    # apply は現在の逐語録上の表記に対して行う
                    "word": surface,
                    "detected_word": word,
                    "context": display,
                    "display": display,
                    "estimated_correction": candidate,
                    "anomaly_type": str(p.get("anomaly_type") or "").strip(),
                    "reason": str(p.get("reason") or "").strip()[:80],
                    "location": str(resolved.get("location") or ""),
                    "position": int(resolved.get("position") or -1),
                    "found_in_transcript": bool(resolved.get("found_in_transcript")),
                },
            )
        )
    ranked.sort(key=lambda x: x[0])
    items = [item for _, item in ranked[:limit]]
    return _merge_items_with_same_candidate(items)


def _merge_items_with_same_candidate(items: list[dict]) -> list[dict]:
    """同一の修正候補を持つ項目を1問に統合する（2026-08-05 ユーザー指摘）。

    「山谷さん」「謝さん」はどちらも候補が「山屋さん」で、別々に聞いても
    結論は同じ。1項目に統合し、回答は merged_words の全 word にも適用する。
    候補なし・span_hypothesis は統合対象外。
    """
    by_candidate: dict[str, dict] = {}
    out: list[dict] = []
    for it in items:
        cand = str(it.get("estimated_correction") or "").strip()
        if not cand or str(it.get("question_kind") or "") == "span_hypothesis":
            out.append(it)
            continue
        primary = by_candidate.get(cand)
        if primary is None:
            by_candidate[cand] = it
            out.append(it)
            continue
        word = str(it.get("word") or "").strip()
        if word and word != str(primary.get("word") or ""):
            primary.setdefault("merged_words", []).append(word)
        aid = str(it.get("anomaly_id") or "").strip()
        if aid:
            primary.setdefault("merged_anomaly_ids", []).append(aid)
    return out


# ---------------------------------------------------------------------------
# 影響トリアージ（2026-08-05 ユーザー決定）:
# 質問数は確信度ではなく「間違えたときの被害」で仕分ける。
# - 文脈からほぼ一意に定まる意味保存的な訂正（移動→異動 等）→ 自動適用
# - 発言の意味・事実を変えうる仮説復元、削除判断、数値・人名 → 質問
# ---------------------------------------------------------------------------

_TRIAGE_MODEL = "claude-sonnet-5"
_DIGIT_RE = re.compile(r"[0-9０-９]")
_HONORIFIC_NAME_TRIAGE_RE = re.compile(
    r"[\u4E00-\u9FFF\u30A0-\u30FF]{1,4}(さん|様|氏|くん|ちゃん)"
)


def _deterministic_ask_reason(item: dict) -> str | None:
    """LLM を呼ぶまでもなく「質問すべき」と判るケースの理由を返す。"""
    word = str(item.get("word") or "").strip()
    candidate = str(item.get("estimated_correction") or "").strip()
    if not candidate:
        return "no_candidate"
    if candidate == "削除" or str(item.get("question_kind") or "") == "span_hypothesis":
        return "delete_or_span_hypothesis"
    if _DIGIT_RE.search(word) or _DIGIT_RE.search(candidate):
        return "numeric"
    if _HONORIFIC_NAME_TRIAGE_RE.search(word) or _HONORIFIC_NAME_TRIAGE_RE.search(
        candidate
    ):
        return "person_name"
    # 長い語は「語の言い換え」ではなく発言の復元仮説である可能性が高い
    if len(word) >= 12 or len(candidate) >= 12:
        return "long_span_hypothesis"
    return None


def triage_batch_items_for_auto_apply(
    items: list[dict],
    *,
    meeting_profile: dict | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """バッチ項目を (自動適用, 質問) に仕分ける。

    返り値: (auto_items, ask_items, audit_records)。
    LLM 判定が失敗した場合は全件質問（安全側・従来動作）。
    """
    ask_items: list[dict] = []
    eligible: list[dict] = []
    audit: list[dict] = []
    for item in items:
        reason = _deterministic_ask_reason(item)
        if reason:
            ask_items.append(item)
            audit.append(
                {
                    "word": item.get("word"),
                    "candidate": item.get("estimated_correction"),
                    "verdict": "ask",
                    "reason": reason,
                }
            )
        else:
            eligible.append(item)
    if not eligible:
        return [], ask_items, audit

    verdicts: dict[int, dict] = {}
    try:
        import os

        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("no_api_key")
        client = anthropic.Anthropic()
        lines = []
        for i, item in enumerate(eligible, 1):
            ctx = str(item.get("display") or item.get("context") or "")[:200]
            lines.append(
                f"{i}. 語:「{item.get('word')}」 候補:「{item.get('estimated_correction')}」"
                f" 文脈: {ctx}"
            )
        profile_hint = ""
        if meeting_profile:
            title = str(meeting_profile.get("meeting_title") or "").strip()
            if title:
                profile_hint = f"\n会議: {title}"
        resp = client.messages.create(
            model=_TRIAGE_MODEL,
            max_tokens=1500,
            timeout=60,
            system=(
                "あなたは議事録の音声認識訂正候補を仕分けます。"
                "各項目について、候補への置換が「文脈からほぼ一意に定まる"
                "意味保存的な訂正」（例: 移動→異動、習字→週次、戦績表→成績表 の"
                "ような同音・類音の誤変換で、文脈上ほかの解釈が考えにくいもの）"
                "なら auto、そうでないもの（発言の意味・内容が変わりうる推測、"
                "文の復元仮説、複数の解釈がありうるもの）は ask としてください。"
                "迷ったら必ず ask。"
                '出力はJSON配列のみ: [{"index":1,"verdict":"auto|ask","reason":"短く"}]'
            ),
            messages=[{"role": "user", "content": "\n".join(lines) + profile_hint}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        )
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            for row in json.loads(m.group(0)):
                idx = int(row.get("index") or 0)
                if 1 <= idx <= len(eligible):
                    verdicts[idx] = row
    except Exception as exc:  # noqa: BLE001
        print(f"batch_triage_llm_failed={exc!r}")
        verdicts = {}

    auto_items: list[dict] = []
    for i, item in enumerate(eligible, 1):
        row = verdicts.get(i) or {}
        verdict = str(row.get("verdict") or "ask").strip().lower()
        record = {
            "word": item.get("word"),
            "candidate": item.get("estimated_correction"),
            "verdict": "auto" if verdict == "auto" else "ask",
            "reason": str(row.get("reason") or "llm_unavailable_or_ask")[:80],
        }
        audit.append(record)
        if verdict == "auto":
            auto_items.append(item)
        else:
            ask_items.append(item)
    return auto_items, ask_items, audit


def auto_apply_triaged_items(
    *,
    job_dir: str,
    transcript_path: str,
    auto_items: list[dict],
    audit_records: list[dict] | None = None,
) -> int:
    """意味保存的と判定された訂正を本文へ適用し、unknown_points を確定させる。

    回答済み扱い（answer に自動適用と明記）にすることで、カスケード知識にも
    乗り、次サイクルで再質問されない。返り値: 適用件数。
    """
    import os
    from datetime import datetime, timezone

    if not auto_items or not os.path.isfile(transcript_path):
        return 0
    with open(transcript_path, "r", encoding="utf-8") as f:
        text = f.read()
    parsed = [
        {
            "index": i,
            "word": str(item.get("word") or ""),
            "action": "correct",
            "correction": str(item.get("estimated_correction") or ""),
            "anomaly_id": str(item.get("anomaly_id") or ""),
        }
        for i, item in enumerate(auto_items, 1)
    ]
    new_text, applied = apply_batch_corrections(text, parsed)
    if not applied:
        return 0
    with open(transcript_path, "w", encoding="utf-8") as f:
        f.write(new_text)

    applied_ids = {str(a.get("anomaly_id") or "") for a in applied}
    applied_words = {
        str(a.get("word") or a.get("before") or "") for a in applied
    }
    now_iso = datetime.now(timezone.utc).isoformat()
    unknowns_path = os.path.join(job_dir, "unknown_points.json")
    if os.path.isfile(unknowns_path):
        try:
            with open(unknowns_path, "r", encoding="utf-8") as f:
                points = json.load(f)
            changed = False
            for p in points:
                if not isinstance(p, dict):
                    continue
                pid = str(p.get("anomaly_id") or "")
                pword = str(p.get("anomaly_word") or "")
                if (pid and pid in applied_ids) or (
                    pword and pword in applied_words
                ):
                    corr = next(
                        (
                            str(a.get("after") or "")
                            for a in applied
                            if str(a.get("anomaly_id") or "") == pid
                            or str(a.get("word") or a.get("before") or "")
                            == pword
                        ),
                        "",
                    )
                    p["status"] = "answered"
                    p["answer"] = (
                        f"自動適用（意味保存的訂正）: {pword}→{corr}"
                    )
                    p["answered_by_question_id"] = "auto_triage"
                    p["answered_at"] = now_iso
                    changed = True
            if changed:
                with open(unknowns_path, "w", encoding="utf-8") as f:
                    json.dump(points, f, ensure_ascii=False, indent=2)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"auto_triage_unknowns_update_failed={exc!r}")

    try:
        audit_path = os.path.join(job_dir, "auto_triage_audit.jsonl")
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "at": now_iso,
                        "applied": [
                            {
                                "word": a.get("word") or a.get("before"),
                                "after": a.get("after"),
                            }
                            for a in applied
                        ],
                        "triage": audit_records or [],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError as exc:
        print(f"auto_triage_audit_write_failed={exc!r}")
    return len(applied)


def build_batch_question_text(items: list[dict]) -> str:
    """番号付きの一括確認メッセージ本文を組み立てる。

    現在の逐語録から抜いた引用＋【該当語】＋修正候補。
    要約議事録には載らない旨を明示し、Doc検索で迷わないようにする。
    """
    lines = [
        "【重要】これは文字起こし原文（逐語録）の表記確認です。",
        "Googleドキュメントの要約・決定事項・議題には出てきません。",
        "Doc内検索では見つからないので、下の引用文だけを見て回答してください。",
        "",
        "番号ごとに返信:",
        "・候補どおり / そのままで正しい →「OK」",
        "・違う語 → 正しい表記",
        "・不要 →「削除」 / 分からない →「不明」",
        "",
    ]
    for i, it in enumerate(items, 1):
        word = str(it.get("word") or "").strip()
        loc = str(it.get("location") or "").strip()
        loc_part = f"（逐語録{loc}）" if loc else "（逐語録）"
        if str(it.get("question_kind") or "") == "span_hypothesis":
            # 文単位の仮説確認: 崩壊した発言を引用し、復元仮説ごと聞く
            hypo = str(it.get("estimated_correction") or "").strip()
            quote = word if len(word) <= 160 else word[:157] + "…"
            lines.append(f"{i}.{loc_part}意味が取りにくい発言:")
            lines.append(f"　「{quote}」")
            if hypo:
                lines.append(f"　→ 仮説:「{hypo}」という趣旨でしょうか？")
                lines.append("　　OK=仮説どおり / 違えば正しい言い回し / 削除=発言ごと不要 / 不明")
            else:
                lines.append("　→ どういう発言だったか教えてください（言い回し / 削除 / 不明）")
            continue
        display = str(it.get("display") or it.get("context") or "").strip()
        if not display:
            display = word
        candidate = str(it.get("estimated_correction") or "").strip()
        detected = str(it.get("detected_word") or "").strip()
        lines.extend(
            _format_batch_word_item_lines(
                index=i,
                loc_part=loc_part,
                word=word,
                display=display,
                candidate=candidate,
                detected=detected,
            )
        )
        merged_words = [
            str(w).strip() for w in (it.get("merged_words") or []) if str(w).strip()
        ]
        if merged_words and candidate:
            lines.append(
                f"　※次の表記も同じ「{candidate}」の誤変換と思われます"
                "（回答は全箇所に適用されます）:"
            )
            for w in merged_words:
                lines.append(f"　　・【{w}】")
    lines.append("")
    lines.append("例) 1 OK / 2 稟議決裁 / 3 削除 / 4 不明")
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


def _is_deletion_only_edit(original: str, edited: str) -> bool:
    """edited が original からの「削除のみ」で得られるか（挿入・言い換え禁止）。"""
    import difflib

    o = str(original or "")
    e = str(edited or "")
    if not e or len(e) >= len(o):
        return False
    sm = difflib.SequenceMatcher(None, o, e, autojunk=False)
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace") and (j2 - j1) > 0:
            return False
    return True


def _smart_delete_sentence_via_llm(
    sentence: str, word: str, *, api_key: str, model: str = "gpt-4.1", timeout_sec: int = 60
) -> str | None:
    """文から誤認識断片だけを最小限に取り除いた文を返す（失敗時 None）。

    出力は「元の文からの削除のみ」で構成されていることを difflib で厳格検証する。
    1文字でも挿入・言い換えがあれば不採用（幻覚防止）。
    """
    system_prompt = (
        "あなたは文字起こしの校正者です。与えられた文から、指定された"
        "音声認識の誤変換断片（と、それに直接付随して意味をなさなくなる助詞・"
        "接続の最小限の断片のみ）を取り除いてください。"
        "\n厳守事項:"
        "\n- 出力は編集後の文のみ。説明・前置き・引用符は禁止。"
        "\n- 残す部分は一字一句変更しない（言い換え・追加・順序変更は禁止）。"
        "\n- 削除は必要最小限。実質的な発言内容は残す。"
        "\n- 取り除くと文が成立しない場合は、元の文をそのまま出力する。"
    )
    user_payload = {"sentence": sentence, "remove_fragment": word}
    try:
        resp = requests.post(
            _OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "temperature": 0.0,
                "max_output_tokens": 1000,
                "input": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            },
            timeout=timeout_sec,
        )
        if resp.status_code != 200:
            return None
        edited = _extract_output_text(resp.json()).strip()
    except Exception:  # noqa: BLE001
        return None
    if not edited or word in edited:
        return None
    if not _is_deletion_only_edit(sentence, edited):
        return None
    return edited


def _apply_delete_to_transcript(
    transcript: str, *, span_hint: str, word: str, api_key: str | None = None
) -> tuple[str, dict | None]:
    w = str(word or "").strip()
    # スマート削除: 該当文からガーブル断片だけを最小限取り除く（削除のみ検証付き）。
    # 文まるごと削除（従来）は実発言まで巻き込むため、可能な限りこちらを使う。
    if api_key and w:
        idx = find_standalone_word(transcript, w)
        if idx < 0:
            idx = transcript.find(w)
        if idx >= 0:
            s, e = _expand_to_sentence_span(transcript, idx, len(w))
            sentence = transcript[s:e]
            edited = _smart_delete_sentence_via_llm(sentence, w, api_key=api_key)
            if edited is not None and edited != sentence:
                out = transcript[:s] + edited + transcript[e:]
                return out, {
                    "before": sentence.strip(),
                    "after": edited.strip(),
                    "action": "delete",
                    "mode": "smart_fragment",
                    "word": w,
                }
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
    "現状維持",
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


# バッチ回答で「削除」は delete 指示。correct の correction として解釈しない。
_DELETE_LITERAL_TOKENS = frozenset({"削除", "delete", "消して"})


def _coerce_parsed_row_action(
    action: str, correction: str, *, word: str
) -> tuple[str, str]:
    """correct+correction=『削除』は語の置換ではなく delete 指示への誤解釈。"""
    a = str(action or "unknown").strip().lower()
    c = str(correction or "").strip()
    w = str(word or "").strip()
    if a == "correct":
        if c in _DELETE_LITERAL_TOKENS and c != w:
            return "delete", ""
        if c.lower() in {"delete"} and w.lower() != "delete":
            return "delete", ""
    return a, c


def _finalize_batch_item_action(
    action: str,
    correction: str,
    token: str,
    item: dict,
) -> tuple[str, str]:
    """バッチ質問で候補提示ありのとき、OK は候補採用(correct)とみなす。

    質問文は「候補どおり→OK」と「そのままで正しい→OK」の両方に OK を使うが、
    estimated_correction がある項目でユーザーが OK と答えた場合は候補を採用する。
    """
    cand = str(item.get("estimated_correction") or "").strip()
    surface_raw = _normalize_answer_surface(token).strip()
    compact_raw = surface_raw.replace(" ", "")
    ok_suffix = re.fullmatch(r"(.+?)(?:OK|ＯＫ|ok|okay)", compact_raw)
    if ok_suffix:
        approved = ok_suffix.group(1).strip()
        word = str(item.get("word") or "").strip()
        if cand and approved == cand.replace(" ", ""):
            if str(item.get("question_kind") or "") == "span_hypothesis":
                cand = sanitize_hypothesis_fillers(cand)
            return "correct", cand
        if word and approved == word.replace(" ", ""):
            return "keep", ""
    if action != "keep" or not cand:
        return action, correction
    surface = surface_raw.lower().replace(" ", "")
    if surface in {"ok", "okay"}:
        if str(item.get("question_kind") or "") == "span_hypothesis":
            cand = sanitize_hypothesis_fillers(cand)
        return "correct", cand
    return action, correction


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


def _batch_rows_from_numbered_matches(
    matches: dict[int, str], items: list[dict]
) -> list[dict]:
    out: list[dict] = []
    for i, it in enumerate(items, 1):
        token = matches.get(i)
        word = str(it.get("word") or "").strip()
        if token is None:
            action, correction = ("unknown", "")
        else:
            action, correction = _normalize_answer_token(
                token, target_word=word
            )
            action, correction = _finalize_batch_item_action(
                action, correction, token, it
            )
            action, correction = _coerce_parsed_row_action(
                action, correction, word=word
            )
        out.append(
            {
                "anomaly_id": it.get("anomaly_id", ""),
                "word": word,
                "action": action,
                "correction": correction,
                "merged_words": list(it.get("merged_words") or []),
                "merged_anomaly_ids": list(it.get("merged_anomaly_ids") or []),
            }
        )
    return out


def _parse_equals_numbered_answer(answer_text: str, items: list[dict]) -> list[dict] | None:
    """「1=削除、2=相原さん、3=削除」のような = 区切り回答をパースする。"""
    text = str(answer_text or "").strip()
    if not text or ("=" not in text and "＝" not in text):
        return None
    matches: dict[int, str] = {}
    pattern = re.compile(
        r"(?:^|[、,\s])"
        r"(\d{1,2})\s*[=＝]\s*"
        r"([^、,\n]+)"
    )
    for m in pattern.finditer(text):
        idx = int(m.group(1))
        if 1 <= idx <= len(items):
            matches[idx] = (m.group(2) or "").strip()
    if not matches:
        return None
    return _batch_rows_from_numbered_matches(matches, items)


def _parse_numbered_answer_with_regex(answer_text: str, items: list[dict]) -> list[dict] | None:
    """「1 ○○ / 2 OK」のような番号付き回答を素朴にパースする。

    番号が 1 件も拾えなければ None を返し、LLM 解析にフォールバックさせる。
    """
    text = str(answer_text or "").strip()
    if not text:
        return None
    # 区切り(改行 / スラッシュ / 全角スラッシュ / 読点)を跨いで「番号 + 値」を拾う
    # 例: "1 ドライマンゴー", "2.OK", "1=削除", "③ 6ピース診断"
    circled = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    matches: dict[int, str] = {}
    pattern = re.compile(
        r"(?:^|[\n/／、,])\s*"
        r"(?:(\d{1,2})|([" + circled + r"]))"
        r"\s*[\.\):：=＝\.、]?\s*"
        r"([^、,\n/／]+)"
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
    return _batch_rows_from_numbered_matches(matches, items)


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
        "各 item について、ユーザーが正しい表記を指定したか・そのままで良いと言ったか・"
        "削除指示か・不明としたかを判定してください。"
        "\n出力は JSON 配列のみ。各要素は次のキーを持つ:"
        '\n{"index": 整数(itemsのindex),'
        ' "action": "correct"(訂正あり) | "keep"(そのままで良い/OK) | '
        '"delete"(不要・削除) | "unknown"(不明・未回答),'
        ' "correction": "actionがcorrectのときの正しい表記。それ以外は空文字"}'
        "\n判定ルール:"
        "\n- ユーザーが具体的な語を書いていれば correct とし、その語を correction に入れる。"
        "\n- 『OK』『そのまま』『合ってる』等は keep。"
        "\n- 『削除』『消して』『不要』等は delete（correction は空文字）。"
        " delete は語を本文から取り除く指示であり、correction に『削除』と書かない。"
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
        if action not in {"correct", "keep", "unknown", "delete"}:
            action = "unknown"
        correction = str(el.get("correction") or "").strip().strip("「」『』\"'")
        word = str(it.get("word") or "").strip()
        action, correction = _coerce_parsed_row_action(
            action, correction, word=word
        )
        if action == "correct" and not correction:
            action = "unknown"
        if action == "delete":
            correction = ""
        out.append(
            {
                "anomaly_id": it.get("anomaly_id", ""),
                "word": it["word"],
                "action": action,
                "correction": correction,
                "merged_words": list(it.get("merged_words") or []),
                "merged_anomaly_ids": list(it.get("merged_anomaly_ids") or []),
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

    equals_parsed = _parse_equals_numbered_answer(answer_text, items)
    if equals_parsed is not None:
        answered = sum(1 for p in equals_parsed if p["action"] != "unknown")
        if answered >= max(1, len(items) // 2):
            return equals_parsed

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
        {
            "anomaly_id": it.get("anomaly_id", ""),
            "word": it["word"],
            "action": "unknown",
            "correction": "",
            "merged_words": list(it.get("merged_words") or []),
            "merged_anomaly_ids": list(it.get("merged_anomaly_ids") or []),
        }
        for it in items
    ]


def _replace_span_best_effort(text: str, word: str, correction: str) -> tuple[str, bool]:
    """span_hypothesis 向け: 完全一致 → タグ付き → 長い前方一致の順で置換。"""
    if not word or not correction or word == correction:
        return text, False
    tagged = f"{word}{VERIFY_TAG}"
    if tagged in text:
        return text.replace(tagged, correction), True
    if word in text:
        return text.replace(word, correction), True
    if len(word) < 10:
        return text, False
    for length in range(min(len(word), 100), 8, -1):
        prefix = word[:length]
        idx = text.find(prefix)
        if idx < 0:
            continue
        end = min(len(text), idx + len(word))
        replaced_len = end - idx
        return text[:idx] + correction + text[end:], True
    return text, False


def apply_batch_corrections(
    transcript: str, parsed: list[dict], *, api_key: str | None = None
) -> tuple[str, list[dict]]:
    """parsed の指示に従って本文を一括補正し、[要確認] タグを処理する。

    - correct: word(+[要確認]) を correction に置換(全出現)。
    - keep:    word の直後の [要確認] タグだけ除去(語自体は確定でそのまま)。
    - delete:  api_key があれば LLM スマート削除（文からガーブル断片のみ最小除去、
               削除のみ検証付き）。無ければ従来の文スパン削除。
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
        action, correction = _coerce_parsed_row_action(
            action, correction, word=word
        )
        # 同じ語が返ってきた(=表記は正しい)場合は keep と同じく確定扱い(タグ除去)。
        if action == "correct" and (not correction or correction == word):
            action = "keep"
        if action == "delete":
            before = out
            out, deleted = _apply_delete_to_transcript(
                out, span_hint=correction, word=word, api_key=api_key
            )
            if out != before and deleted:
                applied.append(
                    {
                        "anomaly_id": p.get("anomaly_id", ""),
                        "before": deleted.get("before", ""),
                        "after": deleted.get("after", ""),
                        "action": "delete",
                        "mode": deleted.get("mode", "sentence_span"),
                        "word": word,
                    }
                )
            continue
        if action == "correct":
            if correction and len(word) >= 12:
                correction = sanitize_hypothesis_fillers(correction)
            before = out
            # 統合された同一結論の語（merged_words）にも同じ修正を適用する。
            target_words = [word] + [
                str(w).strip()
                for w in (p.get("merged_words") or [])
                if str(w).strip() and str(w).strip() != word
            ]
            # タグ付き・タグなしの両方を全出現置換する。以前は elif で
            # タグ付きが1つでもあるとタグなしの同語をスキップしており、
            # 「山谷さん」が1/3箇所しか直らない実害が出た（2026-08-05）。
            # correction が word を含む場合は二重置換になるため2回目は行わない。
            replaced_any = False
            for w in target_words:
                tagged = f"{w}{VERIFY_TAG}"
                if tagged in out:
                    out = out.replace(tagged, correction)
                    replaced_any = True
                if w in out and w not in correction:
                    out = out.replace(w, correction)
                    replaced_any = True
            if not replaced_any:
                if len(word) >= 10:
                    out, replaced = _replace_span_best_effort(
                        out, word, correction
                    )
                    if not replaced:
                        continue
                else:
                    continue
            if out != before:
                applied.append(
                    {"anomaly_id": p.get("anomaly_id", ""), "before": word,
                     "after": correction, "action": "correct"}
                )
        elif action == "keep":
            keep_words = [word] + [
                str(w).strip()
                for w in (p.get("merged_words") or [])
                if str(w).strip() and str(w).strip() != word
            ]
            for w in keep_words:
                tagged = f"{w}{VERIFY_TAG}"
                if tagged in out:
                    out = out.replace(tagged, w)
                    applied.append(
                        {"anomaly_id": p.get("anomaly_id", ""), "before": tagged,
                         "after": w, "action": "keep"}
                    )
        # unknown: 変更しない
    return out, applied
