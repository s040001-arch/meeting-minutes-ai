import argparse
import anthropic
import httpx
import json
import logging
import os
import platform
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable, Optional

from filename_hints import format_hints_for_prompt
from job_context import format_context_for_prompt
from knowledge_sheet_store import format_knowledge_for_prompt, load_knowledge_memos

logger = logging.getLogger(__name__)

# Claude 4 Opus — environment variable ANTHROPIC_CORRECTION_MODEL overrides this.
OPUS_CORRECTION_MODEL = "claude-opus-4-20250514"

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


def _stream_anthropic_text(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    timeout_sec: int,
    log_label: str,
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
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {message}\n"
    try:
        os.makedirs(os.path.dirname(visible_log_path) or ".", exist_ok=True)
        with open(visible_log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Opus correction prompt
# ---------------------------------------------------------------------------

def _build_opus_correction_system_prompt(
    filename_hints: list[str] | None = None,
    knowledge_memos: list[str] | None = None,
    job_context: dict | None = None,
) -> str:
    """Claude 4 Opus 向け一括補正プロンプト。
    マスキングなしでテキスト全文をそのまま渡す前提。
    補正後テキスト本文のみを出力させる。
    """
    return (
        "あなたは会議の音声認識テキストを補正する専門アシスタントです。"
        "入力テキストは音声認識（Whisper）の出力を機械補正したものです。"
        "以下の補正ルールに従って補正し、補正後のテキスト本文のみを出力してください。"
        "説明文・前置き・注釈は一切付けないでください。"
        "\n\n【補正ルール】"
        "\n1. 音声誤変換を正しい表記に修正する"
        "\n   - 文脈から高い確信度で判断できる場合のみ修正する"
        "\n   - 例：「古車」→「子会社」、「人的尊敬」→「人的資本」、「妖怪じゃないですか」→「要諦じゃないですか」"
        "\n2. 同一テキスト内で同一の対象を指す固有名詞の表記揺れを統一する"
        "\n   - 例：「THR」「thr」「T HR」の混在 → 「THR」に統一"
        "\n3. 明確なフィラー（「えーと」「あのー」などの単独相槌）を削除する"
        "\n4. 言いよどみや自己訂正（「〜じゃなくて、〜」「いや、〜」など）は話者の意図として残す"
        "\n5. 日付・時刻・数値・金額は変更しない"
        "\n6. 要約・内容の追加・削除は禁止"
        "\n7. 不確かな固有名詞・人名・会社名の推測置換は禁止（確信が持てない場合はそのまま残す）"
        + format_hints_for_prompt(filename_hints or [])
        + format_context_for_prompt(job_context or {})
        + format_knowledge_for_prompt(knowledge_memos or [])
    )


# ---------------------------------------------------------------------------
# correct_full_text — public API
# ---------------------------------------------------------------------------

_LAST_CORRECT_FULL_TEXT_META: dict[str, object] = {}


def get_last_correct_full_text_meta() -> dict[str, object]:
    return dict(_LAST_CORRECT_FULL_TEXT_META)


def correct_full_text(
    text: str,
    model: str | None = None,
    timeout_sec: int = 900,
    on_phase: Optional[Callable[[str], None]] = None,
    filename_hints: list[str] | None = None,
    visible_log_path: str | None = None,
    job_context: dict | None = None,
) -> str:
    """機械補正済みテキストを Claude 4 Opus に一括で渡し補正済み全文を返す。

    マスキング・JSON 検出・str.replace の多段処理は廃止。
    テキスト全文をそのままプロンプトに渡すことで文脈を保持する。

    ai_unknown_points は常に空リスト（Step⑨ / ⑩ の Regex 検出が担当）。
    失敗時は元テキストをそのまま返す。
    """
    if not text:
        return text

    resolved_model = (
        model
        or os.environ.get("ANTHROPIC_CORRECTION_MODEL", "").strip()
        or OPUS_CORRECTION_MODEL
    )

    print(f"correct_full_text: input_chars={len(text)} model={resolved_model}")
    global _LAST_CORRECT_FULL_TEXT_META
    max_tokens = min(max(int(len(text) * 1.0), 8192), 40960)
    _LAST_CORRECT_FULL_TEXT_META = {
        "input_chars": len(text),
        "output_chars": 0,
        "stop_reason": None,
        "fallback_reason": None,
        "used_fallback": False,
        "max_tokens": max_tokens,
        "ai_unknown_points": [],
        "ai_unknown_points_count": 0,
    }
    print(f"correct_full_text: request_max_tokens={max_tokens}")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    try:
        # ── ナレッジ読み込み ──────────────────────────────────────────────
        try:
            knowledge_memos = load_knowledge_memos()
            print(f"correct_full_text: knowledge_memos={len(knowledge_memos)}")
            if knowledge_memos:
                _append_visible_log(
                    visible_log_path,
                    f"Step 8: ナレッジ読み込み: {len(knowledge_memos)}件をプロンプトに注入",
                )
            else:
                _append_visible_log(
                    visible_log_path,
                    "Step 8: ナレッジ読み込み: 0件（スキップ）",
                )
        except Exception as e:
            knowledge_memos = []
            print(f"correct_full_text: knowledge_memos_load_failed={e!r}")
            _append_visible_log(
                visible_log_path,
                f"Step 8: ナレッジ読み込み: エラー → {e!r}",
            )

        # ── Opus 一括補正 ─────────────────────────────────────────────────
        if on_phase:
            on_phase("ai_correct")
        _append_visible_log(visible_log_path, "Step 8: AI補正開始（Opus一括）")

        system_prompt = _build_opus_correction_system_prompt(
            filename_hints=filename_hints,
            knowledge_memos=knowledge_memos,
            job_context=job_context,
        )

        full_response, stop_reason = _stream_anthropic_text(
            api_key=api_key,
            model=resolved_model,
            system_prompt=system_prompt,
            user_message=text,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            log_label="AI correction streaming",
        )
        _LAST_CORRECT_FULL_TEXT_META["stop_reason"] = stop_reason

        if stop_reason == "max_tokens":
            _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
            _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = "anthropic_max_tokens_reached"
            print(
                f"[WARNING] correct_full_text: fallback to original. "
                f"reason=anthropic_max_tokens_reached input_chars={len(text)}"
            )
            _append_visible_log(visible_log_path, "Step 8: AI補正: max_tokens 到達 → 元テキスト返却")
            return text

        corrected = full_response.strip()
        if not corrected:
            _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
            _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = "anthropic_text_missing"
            print(
                f"[WARNING] correct_full_text: fallback to original. "
                f"reason=anthropic_text_missing input_chars={len(text)}"
            )
            _append_visible_log(visible_log_path, "Step 8: AI補正: 空レスポンス → 元テキスト返却")
            return text

        _LAST_CORRECT_FULL_TEXT_META["output_chars"] = len(corrected)
        print(
            f"correct_full_text: final_chars={len(corrected)} "
            f"(input was {len(text)}) stop_reason={stop_reason}"
        )
        _append_visible_log(
            visible_log_path,
            f"Step 8: AI補正完了 output={len(corrected)} stop_reason={stop_reason}",
        )
        return corrected

    except httpx.TimeoutException as e:
        _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
        _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = f"timeout:{e!r}:timeout_sec={timeout_sec}"
        print(
            f"[WARNING] correct_full_text: fallback to original. "
            f"reason=timeout:{e!r} timeout_sec={timeout_sec} input_chars={len(text)}"
        )
        _append_visible_log(visible_log_path, f"Step 8: AI補正: タイムアウト → 元テキスト返却")
        return text
    except httpx.HTTPError as e:
        _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
        _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = f"http_error:{e!r}"
        print(
            f"[WARNING] correct_full_text: fallback to original. "
            f"reason=http_error:{e!r} input_chars={len(text)}"
        )
        _append_visible_log(visible_log_path, f"Step 8: AI補正: HTTP エラー → 元テキスト返却")
        return text
    except Exception as e:
        _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
        _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = f"exception:{e!r}"
        print(
            f"[WARNING] correct_full_text: fallback to original. "
            f"reason=exception:{e!r} input_chars={len(text)}"
        )
        _append_visible_log(visible_log_path, f"Step 8: AI補正: 例外 → 元テキスト返却 → {e!r}")
        return text


# ---------------------------------------------------------------------------
# Legacy OpenAI helpers (used by recorrect_with_answer.py / recorrect_from_line_answer.py)
# ---------------------------------------------------------------------------

def call_openai_incorporate_answer(
    text: str,
    question_text: str,
    answer_text: str,
    model: str,
    api_key: str,
    timeout_sec: int = 120,
    *,
    excerpt_mode: bool = False,
) -> str:
    """ユーザー回答を補正済み全文へ反映したうえで、再度整形する。
    excerpt_mode が True のときは text を発言録の一部として扱い、同範囲の更新後テキストのみを返す。
    """
    url = "https://api.openai.com/v1/responses"
    user_block = (
        "### 発言録\n"
        f"{text}\n\n"
        "### 確認していた質問\n"
        f"{question_text}\n\n"
        "### ユーザーの回答\n"
        f"{answer_text}"
    )
    if excerpt_mode:
        system_content = (
            "あなたは議事録整形アシスタントです。"
            "与えられた発言録は長文の一部抜粋です。確認質問へのユーザーの回答内容を反映して、この抜粋範囲のみを更新してください。"
            "回答が指す固有名詞・数値・主語などを正しく差し替え・補完し、それ以外の事実や文意は変えないでください。"
            "抜粋の前後に続く文脈と矛盾しないよう、この部分の表現だけを整えてください。"
            "不要な説明は出力せず、更新後の抜粋テキストのみを返してください（発言録の続きや前置きは付けないでください）。"
        )
    else:
        system_content = (
            "あなたは議事録整形アシスタントです。"
            "与えられた発言録全文に対し、確認質問へのユーザーの回答内容を反映して更新してください。"
            "回答が指す固有名詞・数値・主語などを正しく差し替え・補完し、それ以外の事実や文意は変えないでください。"
            "不要な説明は出力せず、更新後の発言録全文のみ返してください。"
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
    return updated


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
