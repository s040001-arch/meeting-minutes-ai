import argparse
import json
import os
import re
import time
from typing import Any, Tuple

import anthropic
import httpx


_MINUTES_MODEL = "claude-sonnet-4-20250514"
_MINUTES_TIMEOUT_SEC = 900
_MINUTES_RETRY_BACKOFF_SEC = (5.0, 10.0)


def extract_title_and_transcript(minutes_draft_text: str) -> Tuple[str, str]:
    lines = minutes_draft_text.splitlines()
    title = "minutes"
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip() or title
            break

    transcript_lines = []
    in_transcript = False
    for line in lines:
        if not in_transcript:
            if line.strip() in {"## 発言録", "##発言録", "## 発言録（逐語）", "##発言録（逐語）"}:
                in_transcript = True
            continue
        transcript_lines.append(line)

    transcript = "\n".join(transcript_lines).strip()
    if not transcript:
        raise ValueError("minutes draft does not contain transcript section: `## 発言録`.")
    return title, transcript


def _build_minutes_system_prompt() -> str:
    return (
        "あなたは日本語の議事録作成アシスタントです。"
        "与えられた発言録だけを根拠に、会議メモの要点を整理してください。"
        "推測や創作は禁止です。根拠が弱い情報は書かず、空配列にしてください。"
        "出力は必ずJSONオブジェクトのみとし、説明文・コードフェンスは禁止です。"
        'キーは "participants", "agenda", "decisions", "open_issues", "next_actions" の5つだけを使ってください。'
        "各値は文字列配列です。"
        "participants には参加者候補、agenda には議題、decisions には決定事項、"
        "open_issues には残論点、next_actions には次アクションを入れてください。"
        "発言録本文そのものは出力しないでください。"
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("minutes generation output is not a JSON object")
    return data


def _normalize_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in items:
            items.append(text)
    return items


def _format_bullets(items: list[str]) -> str:
    if not items:
        return "- （未設定）"
    return "\n".join(f"- {item}" for item in items)


def _build_minutes_structured_md(title: str, transcript_md: str, sections: dict[str, Any]) -> str:
    participants = _normalize_items(sections.get("participants"))
    agenda = _normalize_items(sections.get("agenda"))
    decisions = _normalize_items(sections.get("decisions"))
    open_issues = _normalize_items(sections.get("open_issues"))
    next_actions = _normalize_items(sections.get("next_actions"))
    transcript = transcript_md.strip()
    if not transcript:
        transcript = "（未設定）"
    return (
        f"# {title}\n\n"
        "## 参加者\n\n"
        f"{_format_bullets(participants)}\n\n"
        "## 議題\n\n"
        f"{_format_bullets(agenda)}\n\n"
        "## 決定事項\n\n"
        f"{_format_bullets(decisions)}\n\n"
        "## 残論点\n\n"
        f"{_format_bullets(open_issues)}\n\n"
        "## Next Action\n\n"
        f"{_format_bullets(next_actions)}\n\n"
        "## 発言録（逐語）\n\n"
        f"{transcript}\n"
    )


def _generate_minutes_sections_with_claude(
    *,
    title: str,
    transcript_md: str,
    model: str,
    timeout_sec: int,
) -> dict[str, Any]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(timeout=float(timeout_sec), connect=30.0),
    )
    user_message = json.dumps(
        {
            "title": title,
            "transcript": transcript_md,
            "output_keys": [
                "participants",
                "agenda",
                "decisions",
                "open_issues",
                "next_actions",
            ],
        },
        ensure_ascii=False,
    )

    last_error: Exception | None = None
    max_attempts = len(_MINUTES_RETRY_BACKOFF_SEC) + 1
    for attempt in range(1, max_attempts + 1):
        started_at = time.monotonic()
        print(
            f"minutes_generation_claude_started attempt={attempt}/{max_attempts} "
            f"input_chars={len(user_message)} model={model}"
        )
        full_response = ""
        try:
            with client.messages.stream(
                model=model,
                max_tokens=4000,
                system=_build_minutes_system_prompt(),
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                for text_chunk in stream.text_stream:
                    full_response += text_chunk
            elapsed = time.monotonic() - started_at
            print(
                f"minutes_generation_claude_completed attempt={attempt}/{max_attempts} "
                f"output_chars={len(full_response)} elapsed={elapsed:.1f}s"
            )
            return _extract_json_object(full_response)
        except Exception as e:  # noqa: BLE001
            last_error = e
            elapsed = time.monotonic() - started_at
            print(
                f"minutes_generation_claude_failed attempt={attempt}/{max_attempts} "
                f"elapsed={elapsed:.1f}s error={e!r}"
            )
            if attempt >= max_attempts:
                break
            backoff_sec = _MINUTES_RETRY_BACKOFF_SEC[attempt - 1]
            print(
                f"minutes_generation_claude_retrying next_attempt={attempt + 1}/{max_attempts} "
                f"backoff_sec={backoff_sec:.1f}"
            )
            time.sleep(backoff_sec)
    raise RuntimeError(f"minutes generation failed: {last_error!r}")


def resolve_input_path(job_id: str, input_path: str | None, input_root: str) -> str:
    if input_path:
        return input_path
    job_dir = os.path.join(input_root, job_id)
    for name in ("minutes_draft.md", "minutes_draft.txt"):
        p = os.path.join(job_dir, name)
        if os.path.isfile(p):
            return p
    return os.path.join(job_dir, "minutes_draft.md")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Task 6-2: Claude で議事録の要約セクションを生成し、構造化Markdownを出力"
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input",
        default=None,
        help="入力（未指定時: {input_root}/{job_id}/minutes_draft.md）",
    )
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="ジョブディレクトリのルート（デフォルト: data/transcriptions）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="出力先（未指定時: {input_root}/{job_id}/minutes_structured.md）",
    )
    parser.add_argument(
        "--model",
        default=_MINUTES_MODEL,
        help=f"Claude model name (default: {_MINUTES_MODEL})",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=_MINUTES_TIMEOUT_SEC,
        help=f"API timeout seconds (default: {_MINUTES_TIMEOUT_SEC})",
    )
    args = parser.parse_args()

    in_path = resolve_input_path(args.job_id, args.input, args.input_root)
    if not os.path.isfile(in_path):
        raise FileNotFoundError(f"input file not found: {in_path}")

    with open(in_path, "r", encoding="utf-8") as f:
        draft_text = f.read()

    title, transcript_md = extract_title_and_transcript(draft_text)
    sections = _generate_minutes_sections_with_claude(
        title=title,
        transcript_md=transcript_md,
        model=args.model,
        timeout_sec=args.timeout_sec,
    )
    output_text = _build_minutes_structured_md(
        title=title,
        transcript_md=transcript_md,
        sections=sections,
    )

    out_path = args.output or os.path.join(
        args.input_root, args.job_id, "minutes_structured.md"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output_text)

    print(f"job_id={args.job_id}")
    print(f"input={in_path}")
    print(f"output={out_path}")
    print(f"model={args.model}")
    print("status=minutes_structured_generated")


if __name__ == "__main__":
    main()

