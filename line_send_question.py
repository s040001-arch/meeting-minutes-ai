import argparse
import json
import os
import urllib.error
import urllib.request

from repo_env import load_dotenv_local


LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def load_question_result(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("question result file must be a JSON object.")
    return data


def build_line_message(result: dict) -> str:
    job_id = str(result.get("job_id", ""))
    status = str(result.get("question_status", "none"))
    if status == "generated":
        question_text = str(result.get("question_text", "")).strip()
        qid = str(result.get("question_id", "")).strip()
        head = f"[質問あり]\njob_id={job_id}\n"
        if qid:
            head += f"question_id={qid}\n"
        return f"{head}{question_text}"
    return f"[質問なし]\njob_id={job_id}\n不明箇所は0件のため確認事項はありません。"


def push_line_message(channel_access_token: str, user_id: str, text: str) -> None:
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": text}],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=LINE_PUSH_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            _ = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LINE API error: {e.code} {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"LINE API connection error: {e}") from e


def main() -> None:
    load_dotenv_local()
    parser = argparse.ArgumentParser(
        description="Task 5-2: Task 5-1結果JSONを読み込み、LINE_USER_IDへ送信"
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input",
        default=None,
        help="Task 5-1結果JSON（未指定時: data/transcriptions/{job_id}/question_result.json）",
    )
    args = parser.parse_args()

    input_path = args.input or os.path.join(
        "data", "transcriptions", args.job_id, "question_result.json"
    )
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"question result file not found: {input_path}")

    line_user_id = os.getenv("LINE_USER_ID")
    line_channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    if not line_user_id:
        raise RuntimeError("LINE_USER_ID is not set.")
    if not line_channel_access_token:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN is not set.")

    result = load_question_result(input_path)
    message_text = build_line_message(result)

    push_line_message(
        channel_access_token=line_channel_access_token,
        user_id=line_user_id,
        text=message_text,
    )

    print(f"job_id={args.job_id}")
    print(f"input={input_path}")
    print(f"line_user_id={line_user_id}")
    print(f"question_status={result.get('question_status', 'none')}")
    print("line_push=sent")


if __name__ == "__main__":
    main()

