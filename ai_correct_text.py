import argparse
import anthropic
import httpx
import json
import logging
import os
import platform
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable, Optional

from job_context import format_context_for_prompt
from knowledge_sheet_store import format_knowledge_for_prompt, load_knowledge_memos
from meeting_profile import format_meeting_profile_for_prompt
from mechanical_correct_text import apply_pixel_recognizer_fixes
from pipeline_build import get_pipeline_build_info

logger = logging.getLogger(__name__)

# Claude Opus — environment variable ANTHROPIC_CORRECTION_MODEL overrides this.
OPUS_CORRECTION_MODEL = "claude-opus-4-7"
OPUS_MAX_OUTPUT_TOKENS = 128_000
_CORRECTION_CHUNK_TARGET_CHARS = 7000
_CORRECTION_MIN_LENGTH_RATIO_DEFAULT = 0.85
CORRECTION_META_FILENAME = "correction_meta.json"


def resolve_correction_model(model: str | None = None) -> str:
    return (
        model
        or os.environ.get("ANTHROPIC_CORRECTION_MODEL", "").strip()
        or OPUS_CORRECTION_MODEL
    )

_STREAM_MAX_RETRIES = 2
_STREAM_CHUNK_PREVIEW_CHARS = 80
_STREAM_LOG_EVERY_CHARS = 500
_STREAM_LOG_EVERY_SEC = 10.0
_STREAM_RETRY_BACKOFF_SEC = (5.0, 10.0)


# ---------------------------------------------------------------------------
# API key utilities
# ---------------------------------------------------------------------------

def _normalize_api_key(raw: str | None) -> str | None:
    if not raw:
        return None
    v = raw.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1].strip()
    return v or None


def _load_api_key_from_windows_user_env() -> str | None:
    if platform.system().lower() != "windows":
        return None
    try:
        import winreg  # type: ignore

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment")
        value, _ = winreg.QueryValueEx(key, "OPENAI_API_KEY")
        winreg.CloseKey(key)
        return _normalize_api_key(value)
    except Exception:
        return None


def resolve_openai_api_key() -> tuple[str | None, str]:
    # 1) 現在プロセス環境変数（通常ケース）
    key = _normalize_api_key(os.getenv("OPENAI_API_KEY"))
    if key:
        return key, "process_env"

    # 2) Windows setx直後のフォールバック（ユーザー環境変数レジストリ）
    key = _load_api_key_from_windows_user_env()
    if key:
        return key, "windows_user_env"

    return None, "not_found"


def resolve_input_path(job_id: str, input_path: str | None, input_root: str) -> str:
    if input_path:
        return input_path
    return os.path.join(
        input_root,
        job_id,
        "merged_transcript_mechanical.txt",
    )


# ---------------------------------------------------------------------------
# Streaming helper
# ---------------------------------------------------------------------------

def _is_retryable_stream_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int) and (status_code in {408, 409, 429} or status_code >= 500):
            return True
    return False


def _stream_chunk_preview(text: str) -> str:
    preview = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(preview) > _STREAM_CHUNK_PREVIEW_CHARS:
        preview = preview[:_STREAM_CHUNK_PREVIEW_CHARS] + "..."
    return preview


_VISIBLE_PROGRESS_INTERVAL_SEC = 30.0


def _stream_anthropic_text(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    timeout_sec: int,
    log_label: str,
    on_visible_progress: Optional[Callable[[str], None]] = None,
    input_chars: int = 0,
) -> tuple[str, object | None]:
    max_attempts = _STREAM_MAX_RETRIES + 1
    for attempt in range(1, max_attempts + 1):
        client = anthropic.Anthropic(
            api_key=api_key,
            timeout=httpx.Timeout(timeout=float(timeout_sec), connect=30.0),
        )
        started_at = time.monotonic()
        logger.info(
            f"{log_label} started: attempt={attempt}/{max_attempts} "
            f"input_len={len(user_message)} timeout_sec={timeout_sec}"
        )
        full_response = ""
        chunk_count = 0
        next_log_len = _STREAM_LOG_EVERY_CHARS
        last_progress_log_at = started_at
        last_visible_progress_at = started_at
        try:
            with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                for text_chunk in stream.text_stream:
                    chunk_count += 1
                    full_response += text_chunk
                    now = time.monotonic()
                    should_log = len(full_response) >= next_log_len
                    if not should_log and (now - last_progress_log_at) >= _STREAM_LOG_EVERY_SEC:
                        should_log = True
                    if should_log:
                        logger.info(
                            f"{log_label} progress: attempt={attempt}/{max_attempts} "
                            f"chunks={chunk_count} chunk_len={len(text_chunk)} "
                            f"total_len={len(full_response)} "
                            f"preview={_stream_chunk_preview(text_chunk)!r}"
                        )
                        while next_log_len <= len(full_response):
                            next_log_len += _STREAM_LOG_EVERY_CHARS
                        last_progress_log_at = now
                    if on_visible_progress and (now - last_visible_progress_at) >= _VISIBLE_PROGRESS_INTERVAL_SEC:
                        elapsed_sec = int(now - started_at)
                        elapsed_m, elapsed_s = divmod(elapsed_sec, 60)
                        pct = len(full_response) / max(input_chars, 1) * 100 if input_chars else 0
                        pct_str = f"約{min(pct, 99):.0f}%" if input_chars else f"{len(full_response):,}文字受信"
                        on_visible_progress(
                            f"  AI応答を受信中...（{elapsed_m}分{elapsed_s:02d}秒経過、{pct_str}）"
                        )
                        last_visible_progress_at = now
                final_message = stream.get_final_message()
            elapsed = time.monotonic() - started_at
            logger.info(
                f"{log_label} completed: attempt={attempt}/{max_attempts} "
                f"output_len={len(full_response)} chunks={chunk_count} elapsed={elapsed:.1f}s"
            )
            return full_response, getattr(final_message, "stop_reason", None)
        except Exception as e:
            elapsed = time.monotonic() - started_at
            logger.warning(
                f"{log_label} failed: attempt={attempt}/{max_attempts} "
                f"partial_len={len(full_response)} chunks={chunk_count} "
                f"elapsed={elapsed:.1f}s error={e!r}"
            )
            if on_visible_progress and attempt < max_attempts and _is_retryable_stream_error(e):
                on_visible_progress(
                    f"  AI通信エラー → 再試行します（{attempt}/{max_attempts}回目）"
                )
            if attempt >= max_attempts or not _is_retryable_stream_error(e):
                raise
            backoff_idx = min(attempt - 1, len(_STREAM_RETRY_BACKOFF_SEC) - 1)
            backoff_sec = _STREAM_RETRY_BACKOFF_SEC[backoff_idx]
            logger.info(
                f"{log_label} retrying: next_attempt={attempt + 1}/{max_attempts} "
                f"backoff_sec={backoff_sec:.1f}"
            )
            time.sleep(backoff_sec)
    raise RuntimeError(f"{log_label} exhausted retries")


# ---------------------------------------------------------------------------
# Visible log helper
# ---------------------------------------------------------------------------

def _append_visible_log(visible_log_path: str | None, message: str) -> None:
    if not visible_log_path:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    lines = message.split("\n")
    first = f"[{ts}] {lines[0]}"
    rest = [f"           {l}" for l in lines[1:] if l.strip()]
    formatted = "\n".join([first] + rest)
    try:
        os.makedirs(os.path.dirname(visible_log_path) or ".", exist_ok=True)
        with open(visible_log_path, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Opus correction prompt
# ---------------------------------------------------------------------------

def _build_opus_correction_system_prompt(
    meeting_profile: dict | None = None,
    knowledge_memos: list[str] | None = None,
    knowledge_block: str | None = None,
) -> str:
    """Claude 4 Opus 向け一括補正プロンプト。"""
    pixel_block = (
        "\n\n【最優先: Google Pixel 特有の誤変換】"
        "入力は Pixel レコーダーの音声認識テキストです。冒頭から末尾まで均等に注意し、"
        "特に文書の前半で見落としやすい次のパターンを必ず復元してください。"
        "\n- 「最高用」「最高用語」「最高業者」「最高用される」→ 再雇用・再雇用後・再雇用者・再雇用される"
        "\n- 「天然」「天然前」「天然デジャ」→ 定年・定年前・定年で（「天然ガス」はそのまま）"
        "\n- 「食卓」「食卓社員」「食卓定年最高用」→ 嘱託・嘱託社員・嘱託定年再雇用"
        "\n- 「公認育成」→ 後輩育成"
        "\n- 「軽S」「軽装」（経営の意味）→ 経営"
        "\n- 「リネン戦略」「リンチ」「ミンチ政策」→ 理念戦略・認知・認知施策"
        "\n- 「インギージメントサーベリー」「サーベリー」→ エンゲージメントサーベイ・サーベイ"
        "\n- 人名の表記揺れは参加者リストに合わせて統一"
    )
    base = (
        "あなたは会議の音声認識テキストを補正する専門アシスタントです。"
        "入力テキストは Google Pixel レコーダーアプリの音声認識出力を機械補正したものです。"
        "以下の補正ルールに従って補正し、補正後のテキスト本文のみを出力してください。"
        "説明文・前置き・注釈は一切付けないでください。"
        + pixel_block
        + "\n\n【補正ルール】"
        "\n1. 音声誤変換・同音異義語の誤選択を正しい表記に修正する"
        "\n   - 文脈上明らかに不自然な語は、高い確信度で修正する"
        "\n   - 例：「古車」→「子会社」、「人的尊敬」→「人的資本」"
        "\n   - 例：引き継ぎの文脈で「泳ぐ」→「任せ」、「早く学部な人」→「早く若い人」"
        "\n   - 例：「演習さんのところ」→「演習2のところ」、「ベッドはい」→「ベースは」"
        "\n   - 例：「ネストは奥だな」→「ネストは置く／後回し」"
        "\n   - 意味が通らないフレーズ（例：「嬉しく1時間」）は前後文脈から自然な表現に復元"
        "\n2. 同一テキスト内で同一の対象を指す固有名詞の表記揺れを統一する"
        "\n   - 例：「THR」「thr」「T HR」の混在 → 「THR」に統一"
        "\n3. フィラー・無意味な相槌を削除する"
        "\n   - 単独のフィラー（「えーと」「あのー」「えっと」「あの」など）は削除する"
        "\n   - 聞き手の相槌として挟まれた「うん」「はい」「ええ」は、"
        "話の流れに意味を持たない場合は削除してよい"
        "\n   - ただし、質問への明確な返答・同意としての「はい」「うん」は残す"
        "\n   - 文頭の無意味な「あ、」「あっ、」も削除する"
        "\n4. 文の区切りを整理する"
        "\n   - 句点「。」が欠落している長い文は、意味の切れ目で適切に句点を補う"
        "\n   - 1文が極端に長くならないよう、自然な位置で区切る"
        "\n   - ただし話者の発言を分割しすぎないよう注意する"
        "\n5. 発言の途切れや言いかけの表現にはダッシュ「——」を挿入する"
        "\n   - 例：「例えばこのま信頼」→「例えばこのま信頼——」（途中で途切れる箇所）"
        "\n6. 言いよどみや自己訂正（「〜じゃなくて、〜」「いや、〜」など）は話者の意図として残す"
        "\n7. 口語表現・社内造語（「フルフル」「鬼メンタル」「ラポールメーカー」等）はそのまま維持する"
        "\n8. 日付・時刻・数値・金額は変更しない"
        "\n9. 要約・内容の追加・削除は禁止"
        "\n10. 不確かな固有名詞・人名・会社名の推測置換は禁止（確信が持てない場合はそのまま残す）"
        "\n    ただし、同音異義語で文脈上明らかに誤っている一般語（例：引き継ぎ文脈の「泳ぐ」→「任せ」）"
        "はルール1に従い修正する"
        "\n11. 外部会議ではプレセナ側と顧客側の双方が「我々」「御社」「弊社」を使う"
        "\n   - 研修・提案・教材・見積を語る発言はプレセナ側、社内制度・人事施策を語る発言は顧客側"
        "\n   - 一人称・社称の一括置換は禁止。文脈から主語が一意に判断できる場合のみ表記を整える"
        "\n12. 上記 Pixel 誤変換は例示であり、文脈から明らかに別の語を意味する場合は同様の補正を行う。"
        "\n13. 補正前の音声認識特有の冗長な相槌（「うん。」「はい。」の連続、「えっと」「あの」の多用）は、"
        "発言の意味を変えない範囲で適度に整える。ただし話者の意図を改変するような言い換えは行わない。"
    )
    profile_block = format_meeting_profile_for_prompt(meeting_profile or {})
    # Phase 2: knowledge_block(Layer 2 由来の整形済テキスト)を優先。
    # 渡されない場合は従来の memos -> format_knowledge_for_prompt() 経路にフォールバック。
    if knowledge_block is None:
        knowledge_block = format_knowledge_for_prompt(knowledge_memos or [])
    return base + profile_block + (knowledge_block or "")


# ---------------------------------------------------------------------------
# correct_full_text — public API
# ---------------------------------------------------------------------------

_LAST_CORRECT_FULL_TEXT_META: dict[str, object] = {}


def get_last_correct_full_text_meta() -> dict[str, object]:
    return dict(_LAST_CORRECT_FULL_TEXT_META)


def _compute_correction_max_tokens(char_count: int) -> int:
    return min(max(int(char_count * 1.5), 8192), OPUS_MAX_OUTPUT_TOKENS)


_CHUNK_USER_INSTRUCTION = (
    "\n\n【出力要件】"
    "入力の末尾30字程度は表現を変えず、意味を保ったまま必ず出力末尾に含めること。"
    "冒頭から末尾まで均等に補正し、前半の Pixel 誤変換（最高用→再雇用、天然→定年、"
    "食卓→嘱託、公認育成→後輩育成、リネン→理念、インギージメント→エンゲージメント等）を"
    "見落とさないこと。内容の省略・要約は禁止。"
    "\n\n【造語禁止】"
    "\n- 入力テキストに存在しない複合語を新規に生成してはならない。"
    "\n- 特に「〜化」「〜性」「〜的」「〜論」「〜感」「〜観」等の接尾辞を伴う造語に注意。"
    "\n- 同一チャンク内に類似表現が既出の場合は、それに合わせる。"
    "  例: 「自分ごと」が既出なら「自分語化」のような新造語ではなく「自分ごと化」を採用。"
    "\n- 判断に迷う場合は、新しい語を作らず、入力の表現を可能な限り保つこと。"
    "\n- 推測の補完よりも、入力に忠実な復元を優先する。"
)

_CHUNK_TAIL_RETRY_SUFFIX = (
    _CHUNK_USER_INSTRUCTION
    + "\n\n【重要・リトライ】前回の出力では末尾が欠落していました。"
    "入力テキストの先頭から末尾まで、すべて補正して出力してください。途中で止めないでください。"
)

# 会議終了に近い表現（C: 終了表現チェック）
_CLOSING_PHRASES: tuple[str, ...] = (
    "失礼いたします",
    "失礼します",
    "ありがとうございました",
    "ありがとうございます",
    "よいしょ",
    "以上です",
    "お疲れさまでした",
    "お疲れ様でした",
)

# 意味正規化時に除去するフィラー（B）
_FILLER_RE = re.compile(
    r"(?:あのー?|えっと|えーと|えっ|まあ|ふんふん|うんうん)+",
    flags=re.IGNORECASE,
)

_SEMANTIC_ANCHOR_LENGTHS = (80, 60, 45, 30, 22, 18)
_SEMANTIC_ANCHOR_MIN_LEN = 12
_INPUT_TAIL_WINDOW = 400
_OUTPUT_TAIL_WINDOW = 900
_CLOSING_INPUT_TAIL_WINDOW = 800
_CLOSING_OUTPUT_SEARCH_WINDOW = 1200


def _normalize_semantic_text(text: str) -> str:
    """句読点・空白・フィラーを除き、仮名・漢字・数字のみ残す（NFKC で表記揺れを吸収）。"""
    collapsed = _FILLER_RE.sub("", text)
    collapsed = unicodedata.normalize("NFKC", collapsed)
    return re.sub(r"[^\u3040-\u30ff\u4e00-\u9fff0-9]", "", collapsed)


def _closing_phrases_in_text(text: str) -> list[str]:
    return [p for p in _CLOSING_PHRASES if p in text]


def _closing_phrases_covered(input_tail: str, output_text: str) -> bool:
    """入力末尾に終了表現がある場合、出力側にも同表現が残っているか。"""
    in_window = (
        input_tail[-_CLOSING_INPUT_TAIL_WINDOW:]
        if len(input_tail) > _CLOSING_INPUT_TAIL_WINDOW
        else input_tail
    )
    phrases = _closing_phrases_in_text(in_window)
    if not phrases:
        return False

    out_window = (
        output_text[-_CLOSING_OUTPUT_SEARCH_WINDOW:]
        if len(output_text) > _CLOSING_OUTPUT_SEARCH_WINDOW
        else output_text
    )
    norm_out = _normalize_semantic_text(out_window)
    for phrase in phrases:
        if phrase in out_window:
            continue
        norm_phrase = _normalize_semantic_text(phrase)
        if norm_phrase and norm_phrase in norm_out:
            continue
        return False
    return True


def _semantic_tail_anchor_covered(input_chunk: str, output_chunk: str) -> bool:
    """正規化後の入力末尾フレーズが、出力末尾領域に含まれるか（B）。"""
    in_region = (
        input_chunk[-_INPUT_TAIL_WINDOW:]
        if len(input_chunk) > _INPUT_TAIL_WINDOW
        else input_chunk
    )
    out_region = (
        output_chunk[-_OUTPUT_TAIL_WINDOW:]
        if len(output_chunk) > _OUTPUT_TAIL_WINDOW
        else output_chunk
    )
    norm_out = _normalize_semantic_text(out_region)
    norm_in = _normalize_semantic_text(in_region)
    if not norm_in:
        return bool(norm_out)

    for length in _SEMANTIC_ANCHOR_LENGTHS:
        if len(norm_in) < length:
            anchor = norm_in
        else:
            anchor = norm_in[-length:]
        if len(anchor) < _SEMANTIC_ANCHOR_MIN_LEN:
            continue
        if anchor in norm_out:
            return True
    return False


def chunk_output_covers_input_tail(
    input_chunk: str,
    output_chunk: str,
    *,
    is_last_chunk: bool = False,
) -> tuple[bool, str]:
    """補正結果が入力チャンク末尾まで到達しているか（B+C、厳密一致は使わない）。

    Returns:
        (covered, method) — method は correction_meta 用の判定名。
    """
    stripped_in = input_chunk.strip()
    stripped_out = output_chunk.strip()
    if not stripped_in:
        return True, "empty_input"
    if not stripped_out:
        return False, "empty_output"
    if len(stripped_in) < 120:
        ok = len(stripped_out) >= len(stripped_in) * 0.85
        return ok, "short_chunk_ratio" if ok else "none"

    # C: 終了表現（主に最終チャンク／会議末尾）
    if _closing_phrases_covered(stripped_in, stripped_out):
        return True, "closing_phrase"

    # B: 意味正規化した末尾部分一致（中間チャンクの主判定）
    if _semantic_tail_anchor_covered(stripped_in, stripped_out):
        return True, "semantic_anchor"

    # 最終チャンクは終了表現の有無を再確認（入力に無い録音切れは B のみ）
    if is_last_chunk and _closing_phrases_in_text(stripped_in[-_CLOSING_INPUT_TAIL_WINDOW:]):
        return False, "closing_phrase_missing"

    return False, "none"


def _split_text_for_correction(
    text: str,
    target_chars: int = _CORRECTION_CHUNK_TARGET_CHARS,
) -> list[str]:
    """空行（段落）境界で分割し、1チャンクあたり target_chars 字を目安にする。"""
    stripped = text.strip()
    if not stripped:
        return [""]
    if len(stripped) <= target_chars:
        return [stripped]

    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for para in re.split(r"\n\n+", stripped):
        para = para.strip()
        if not para:
            continue
        para_len = len(para) + (2 if current_parts else 0)
        if current_parts and current_len + para_len > target_chars:
            chunks.append("\n\n".join(current_parts))
            current_parts = []
            current_len = 0
        if len(para) > target_chars:
            if current_parts:
                chunks.append("\n\n".join(current_parts))
                current_parts = []
                current_len = 0
            line_buf: list[str] = []
            line_len = 0
            for line in para.split("\n"):
                llen = len(line) + (1 if line_buf else 0)
                if line_buf and line_len + llen > target_chars:
                    chunks.append("\n".join(line_buf))
                    line_buf = []
                    line_len = 0
                line_buf.append(line)
                line_len += llen
            if line_buf:
                chunks.append("\n".join(line_buf))
            continue
        current_parts.append(para)
        current_len += para_len

    if current_parts:
        chunks.append("\n\n".join(current_parts))
    return chunks or [stripped]


def correct_full_text(
    text: str,
    model: str | None = None,
    timeout_sec: int = 900,
    on_phase: Optional[Callable[[str], None]] = None,
    meeting_profile: dict | None = None,
    visible_log_path: str | None = None,
    on_stream_progress: Optional[Callable[[str], None]] = None,
    min_length_ratio: float = _CORRECTION_MIN_LENGTH_RATIO_DEFAULT,
) -> str:
    """機械補正済みテキストを Claude 4 Opus に一括で渡し補正済み全文を返す。

    マスキング・JSON 検出・str.replace の多段処理は廃止。
    テキスト全文をそのままプロンプトに渡すことで文脈を保持する。

    ai_unknown_points は常に空リスト（Step⑨ / ⑩ の Regex 検出が担当）。
    失敗時は元テキストをそのまま返す。
    """
    if not text:
        return text

    resolved_model = resolve_correction_model(model)
    build_info = get_pipeline_build_info()
    env_model_override = os.environ.get("ANTHROPIC_CORRECTION_MODEL", "").strip()

    input_chars = len(text)
    print(
        f"correct_full_text: input_chars={input_chars} model={resolved_model} "
        f"pipeline_correction_version={build_info['pipeline_correction_version']} "
        f"git_commit={build_info['git_commit']}"
    )
    global _LAST_CORRECT_FULL_TEXT_META
    chunks = _split_text_for_correction(text)
    _LAST_CORRECT_FULL_TEXT_META = {
        "input_chars": input_chars,
        "output_chars": 0,
        "ratio": 0.0,
        "stop_reason": None,
        "fallback_reason": None,
        "used_fallback": False,
        "max_tokens": None,
        "model": resolved_model,
        "opus_correction_model_default": OPUS_CORRECTION_MODEL,
        "anthropic_correction_model_env": env_model_override or None,
        "chunk_count": len(chunks),
        "chunk_results": [],
        "min_length_ratio": min_length_ratio,
        "correction_chunk_target_chars": _CORRECTION_CHUNK_TARGET_CHARS,
        "ai_unknown_points": [],
        "ai_unknown_points_count": 0,
        **build_info,
    }
    print(
        f"correct_full_text: chunk_count={len(chunks)} "
        f"min_length_ratio={min_length_ratio}"
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    try:
        # ── ナレッジ読み込み ──────────────────────────────────────────────
        # Phase 2: 関連する Layer 2 セクション(該当顧客/参加者)のみを抽出。
        # Layer 2 が空の場合は内部で legacy free-form memos にフォールバックする。
        try:
            from world_knowledge_store import get_runtime_knowledge_block

            knowledge_block = get_runtime_knowledge_block(
                meeting_profile=meeting_profile, purpose="correction",
            )
            knowledge_memos = []  # ブロックを直接渡すので memos list は空(後段で再使用しない)
            if knowledge_block.strip():
                _append_visible_log(
                    visible_log_path,
                    f"  関連知識を読み込みました（{len(knowledge_block):,}文字）",
                )
            else:
                _append_visible_log(
                    visible_log_path,
                    "  ナレッジシート: 該当する知識なし（スキップ）",
                )
        except Exception as e:
            knowledge_memos = []
            knowledge_block = ""
            print(f"correct_full_text: knowledge_load_failed={e!r}")
            _append_visible_log(
                visible_log_path,
                f"  ナレッジシート読み込みエラー（補正は続行します）: {e!r}",
            )

        if on_phase:
            on_phase("ai_correct")
        _append_visible_log(visible_log_path, "  AIにテキストを送信しました（応答を待っています...）")

        system_prompt = _build_opus_correction_system_prompt(
            meeting_profile=meeting_profile,
            knowledge_memos=knowledge_memos,
            knowledge_block=knowledge_block,
        )

        def _on_stream_visible(msg: str) -> None:
            _append_visible_log(visible_log_path, msg)
            if on_stream_progress:
                on_stream_progress(msg)

        corrected_parts: list[str] = []
        last_stop_reason: object = None
        for idx, chunk in enumerate(chunks, start=1):
            is_last_chunk = idx == len(chunks)
            chunk_max_tokens = _compute_correction_max_tokens(len(chunk))
            print(
                f"correct_full_text: chunk={idx}/{len(chunks)} "
                f"input_chars={len(chunk)} request_max_tokens={chunk_max_tokens}"
            )
            chunk_meta: dict[str, object] = {
                "chunk_index": idx,
                "input_chars": len(chunk),
                "output_chars": 0,
                "ratio": 0.0,
                "stop_reason": None,
                "max_tokens": chunk_max_tokens,
                "tail_covered": False,
                "used_original": False,
                "fallback_reason": None,
                "retry_count": 0,
            }
            adopted_part: str | None = None

            for retry_idx in range(2):
                user_message = (
                    chunk + _CHUNK_USER_INSTRUCTION
                    if retry_idx == 0
                    else chunk + _CHUNK_TAIL_RETRY_SUFFIX
                )
                if retry_idx > 0:
                    chunk_meta["retry_count"] = retry_idx
                    print(
                        f"correct_full_text: chunk {idx} tail anchor retry "
                        f"attempt={retry_idx + 1}/2"
                    )
                full_response, stop_reason = _stream_anthropic_text(
                    api_key=api_key,
                    model=resolved_model,
                    system_prompt=system_prompt,
                    user_message=user_message,
                    max_tokens=chunk_max_tokens,
                    timeout_sec=timeout_sec,
                    log_label=f"AI correction streaming chunk {idx}/{len(chunks)}",
                    on_visible_progress=_on_stream_visible
                    if (visible_log_path or on_stream_progress)
                    else None,
                    input_chars=len(chunk),
                )
                last_stop_reason = stop_reason
                part = full_response.strip()
                chunk_ratio = len(part) / max(len(chunk), 1)
                tail_covered, tail_check_method = chunk_output_covers_input_tail(
                    chunk,
                    part,
                    is_last_chunk=is_last_chunk,
                )
                chunk_meta["output_chars"] = len(part)
                chunk_meta["ratio"] = round(chunk_ratio, 3)
                chunk_meta["stop_reason"] = stop_reason
                chunk_meta["tail_covered"] = tail_covered
                chunk_meta["tail_check_method"] = tail_check_method

                if stop_reason == "max_tokens":
                    print(
                        f"[WARNING] correct_full_text: chunk {idx} hit max_tokens; "
                        "using pixel-fixed mechanical chunk"
                    )
                    chunk_meta["used_original"] = True
                    chunk_meta["fallback_reason"] = "max_tokens"
                    adopted_part = apply_pixel_recognizer_fixes(chunk)
                    break

                if (
                    part
                    and chunk_ratio >= min_length_ratio
                    and tail_covered
                ):
                    adopted_part = apply_pixel_recognizer_fixes(part)
                    break

                if retry_idx == 0 and part and chunk_ratio >= min_length_ratio and not tail_covered:
                    print(
                        f"[WARNING] correct_full_text: chunk {idx} tail not covered "
                        f"(ratio={chunk_ratio:.3f}); retrying once"
                    )
                    continue

                reason = "anthropic_text_missing"
                if part and chunk_ratio < min_length_ratio:
                    reason = "output_too_short"
                elif part and not tail_covered:
                    reason = "tail_not_covered"
                print(
                    f"[WARNING] correct_full_text: chunk {idx} fallback ({reason}) "
                    f"ratio={chunk_ratio:.3f} tail_covered={tail_covered}"
                )
                chunk_meta["used_original"] = True
                chunk_meta["fallback_reason"] = reason
                adopted_part = apply_pixel_recognizer_fixes(chunk)
                break

            _LAST_CORRECT_FULL_TEXT_META["chunk_results"].append(chunk_meta)
            fallback_part = apply_pixel_recognizer_fixes(chunk)
            corrected_parts.append(
                adopted_part if adopted_part is not None else fallback_part
            )

        corrected = "\n\n".join(corrected_parts).strip()
        output_chars = len(corrected)
        ratio = output_chars / max(input_chars, 1)
        _LAST_CORRECT_FULL_TEXT_META["stop_reason"] = last_stop_reason
        _LAST_CORRECT_FULL_TEXT_META["output_chars"] = output_chars
        _LAST_CORRECT_FULL_TEXT_META["ratio"] = round(ratio, 3)

        if not corrected:
            _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
            _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = "anthropic_text_missing"
            _append_visible_log(visible_log_path, "  AI補正: 空の応答が返されたため元テキストを使用")
            return text

        full_tail_covered, full_tail_method = chunk_output_covers_input_tail(
            text, corrected, is_last_chunk=True
        )
        _LAST_CORRECT_FULL_TEXT_META["full_tail_covered"] = full_tail_covered
        _LAST_CORRECT_FULL_TEXT_META["full_tail_check_method"] = full_tail_method
        if not full_tail_covered:
            _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
            _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = "full_text_tail_not_covered"
            print(
                "[WARNING] correct_full_text: fallback to original. "
                "reason=full_text_tail_not_covered"
            )
            _append_visible_log(
                visible_log_path,
                "  AI補正: 全文末尾が欠落しているため元テキストを使用",
            )
            return text

        if ratio < min_length_ratio:
            _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
            _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = (
                f"output_ratio_below_threshold:{ratio:.3f}<{min_length_ratio}"
            )
            print(
                f"[WARNING] correct_full_text: fallback to original. "
                f"reason=output_ratio_below_threshold ratio={ratio:.3f} "
                f"threshold={min_length_ratio} input_chars={input_chars}"
            )
            _append_visible_log(
                visible_log_path,
                f"  AI補正: 出力が短すぎるため元テキストを使用（{ratio:.0%}）",
            )
            return text

        print(
            f"correct_full_text: final_chars={output_chars} "
            f"(input was {input_chars}) ratio={ratio:.3f} stop_reason={last_stop_reason}"
        )
        _append_visible_log(
            visible_log_path,
            f"  AIからの応答を受信しました（{output_chars:,}文字、比率{ratio:.0%}）",
        )
        return corrected

    except httpx.TimeoutException as e:
        _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
        _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = f"timeout:{e!r}:timeout_sec={timeout_sec}"
        print(
            f"[WARNING] correct_full_text: fallback to original. "
            f"reason=timeout:{e!r} timeout_sec={timeout_sec} input_chars={len(text)}"
        )
        _append_visible_log(visible_log_path, "  AI補正: タイムアウトのため元テキストを使用")
        return text
    except httpx.HTTPError as e:
        _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
        _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = f"http_error:{e!r}"
        print(
            f"[WARNING] correct_full_text: fallback to original. "
            f"reason=http_error:{e!r} input_chars={len(text)}"
        )
        _append_visible_log(visible_log_path, "  AI補正: 通信エラーのため元テキストを使用")
        return text
    except Exception as e:
        _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
        _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = f"exception:{e!r}"
        print(
            f"[WARNING] correct_full_text: fallback to original. "
            f"reason=exception:{e!r} input_chars={len(text)}"
        )
        _append_visible_log(visible_log_path, f"  AI補正: エラーが発生したため元テキストを使用 → {e!r}")
        return text


# ---------------------------------------------------------------------------
# Legacy OpenAI helpers (used by recorrect_with_answer.py / recorrect_from_line_answer.py)
# ---------------------------------------------------------------------------

_INCORPORATE_META_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"以下[、,]?.{0,24}残り"),
    re.compile(r"置換[・・]?補完"),
    re.compile(r"全文整形"),
    re.compile(r"反映して.{0,12}(おり|い)ます"),
    re.compile(r"更新後の(?:発言録|抜粋|テキスト)"),
    re.compile(r"作業(?:内容|結果|メモ)"),
    re.compile(r"我々を.{0,40}に置換"),
)


def _looks_like_incorporate_meta_commentary(block: str) -> bool:
    s = block.strip()
    if not s:
        return False
    if any(p.search(s) for p in _INCORPORATE_META_LINE_PATTERNS):
        return True
    if len(s) <= 220 and re.search(
        r"(置換|補完|整形|反映|更新).{0,30}(しました|しており|しています|いたしました)",
        s,
    ):
        return True
    return False


def sanitize_incorporated_transcript_output(
    updated: str,
    original_text: str | None = None,
) -> str:
    """回答反映APIの出力から、作業説明・メタ文・余分な追記を除去する。"""
    text = updated.strip()
    if not text:
        return text

    paragraphs = re.split(r"\n\s*\n", text)
    while paragraphs and _looks_like_incorporate_meta_commentary(paragraphs[-1]):
        paragraphs.pop()
    text = "\n\n".join(p.strip() for p in paragraphs if p.strip()).strip()

    text = re.sub(
        r"\n+(?:以下[、,].*|.*(?:置換[・・]?補完|全文整形).*)$",
        "",
        text,
        flags=re.DOTALL,
    ).strip()

    if original_text:
        orig = original_text.strip()
        if orig and text.startswith(orig):
            tail = text[len(orig) :].lstrip()
            if tail and _looks_like_incorporate_meta_commentary(tail):
                text = orig

    return text


def _build_incorporate_answer_scope_rules(
    scope_quotes: list[str] | None,
    excerpt_mode: bool,
) -> str:
    quote_lines = ""
    if scope_quotes:
        shown = [q.strip() for q in scope_quotes if q and q.strip()]
        if shown:
            quote_lines = (
                "\n【質問で引用された対象箇所（回答の適用はここに限定）】\n"
                + "\n".join(f"- 「{q}」" for q in shown[:3])
            )

    scope_note = (
        "与えられた発言録は長文の一部抜粋です。"
        if excerpt_mode
        else "与えられた発言録全文のうち、質問で引用された箇所とその直前後のみを更新対象とします。"
    )

    return (
        "\n\n【回答の適用範囲（厳守）】\n"
        f"{scope_note}\n"
        "ユーザーの回答は、確認質問内で引用された箇所（「…」）およびその直前後の文脈にのみ適用してください。\n"
        "発言録全体・抜粋全体に回答内容を一律適用してはいけません。\n"
        "特に「我々」「御社」「弊社」「当社」などの一人称・社称は、引用箇所で主語が不明確だった部分を解消するためだけに使います。\n"
        "会議では両側（例: 顧客企業側とコンサル/研修提供側）がそれぞれ「我々」を使うのが普通です。\n"
        "回答で「我々＝A社」と指定されても、文脈上明らかにB社側の発言である箇所の「我々」は変更しないでください。\n"
        "文脈から主語が一意に判断できる箇所では、回答を当てはめず「我々」のまま残して構いません。\n"
        "固有名詞の誤変換修正は、引用箇所に関連する語句に限定してください。"
        f"{quote_lines}"
        "\n\n【出力形式（厳守）】\n"
        "作業説明・置換内容の報告・「以下、残りの発言部分…」などのメタ文は一切出力しないでください。\n"
        "発言録本文のみを返してください。"
    )


def _build_incorporate_answer_system_prompt(
    *,
    excerpt_mode: bool,
    scope_quotes: list[str] | None,
    job_context: dict | None,
) -> str:
    scope_rules = _build_incorporate_answer_scope_rules(scope_quotes, excerpt_mode)
    context_block = format_context_for_prompt(job_context or {})

    if excerpt_mode:
        role = (
            "あなたは議事録整形アシスタントです。"
            "与えられた発言録抜粋に対し、確認質問へのユーザーの回答内容を、引用箇所の文脈に限定して反映してください。"
            "回答が指す固有名詞・数値・主語などを正しく差し替え・補完し、引用箇所と無関係な部分の事実や文意は変えないでください。"
            "抜粋の前後に続く文脈と矛盾しないよう、この部分の表現だけを整えてください。"
            "不要な説明は出力せず、更新後の抜粋テキストのみを返してください（発言録の続きや前置きは付けないでください）。"
        )
    else:
        role = (
            "あなたは議事録整形アシスタントです。"
            "与えられた発言録全文のうち、確認質問で引用された箇所とその直前後のみに、ユーザーの回答内容を反映してください。"
            "引用箇所と無関係な行・段落は一字一句変えないでください。"
            "不要な説明は出力せず、更新後の発言録全文のみ返してください。"
        )

    return role + scope_rules + context_block


def call_openai_incorporate_answer(
    text: str,
    question_text: str,
    answer_text: str,
    model: str,
    api_key: str,
    timeout_sec: int = 120,
    *,
    excerpt_mode: bool = False,
    scope_quotes: list[str] | None = None,
    job_context: dict | None = None,
) -> str:
    """ユーザー回答を補正済み全文へ反映したうえで、再度整形する。
    excerpt_mode が True のときは text を発言録の一部として扱い、同範囲の更新後テキストのみを返す。
    """
    url = "https://api.openai.com/v1/responses"
    if scope_quotes is None:
        scope_quotes = [
            m.group(1).strip()
            for m in re.finditer(r"「([^」]{4,})」", question_text)
            if m.group(1).strip()
        ]

    user_block = (
        "### 発言録\n"
        f"{text}\n\n"
        "### 確認していた質問\n"
        f"{question_text}\n\n"
        "### ユーザーの回答\n"
        f"{answer_text}"
    )
    system_content = _build_incorporate_answer_system_prompt(
        excerpt_mode=excerpt_mode,
        scope_quotes=scope_quotes,
        job_context=job_context,
    )
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": system_content,
            },
            {"role": "user", "content": user_block},
        ],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error: {e.code} {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI API connection error: {e}") from e

    result = json.loads(body)
    output_items = result.get("output", [])
    texts: list[str] = []
    for item in output_items:
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                texts.append(c.get("text", ""))

    updated = "\n".join(t for t in texts if t).strip()
    if not updated:
        raise RuntimeError("OpenAI response did not contain output_text.")
    return sanitize_incorporated_transcript_output(updated, original_text=text)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI補正（Step⑧）: 機械補正済みテキストを Claude 4 Opus で一括補正"
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input",
        default=None,
        help="入力テキスト（未指定時は data/transcriptions/{job_id}/merged_transcript_mechanical.txt）",
    )
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="入力ルートディレクトリ（デフォルト: data/transcriptions）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Anthropic モデル名（デフォルト: {OPUS_CORRECTION_MODEL}）",
    )
    args = parser.parse_args()

    input_path = resolve_input_path(args.job_id, args.input, args.input_root)
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"input file not found: {input_path}")

    with open(input_path, "r", encoding="utf-8") as f:
        original_text = f.read()

    corrected_text = correct_full_text(
        text=original_text,
        model=args.model,
    )

    print(f"job_id={args.job_id}")
    print(f"input={input_path}")
    print("corrected_text=")
    print(corrected_text)


if __name__ == "__main__":
    main()
