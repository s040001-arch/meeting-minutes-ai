import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

from repo_env import load_dotenv_local


def _read_lock_pid(lock_path: str) -> int | None:
    try:
        with open(lock_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("pid="):
                    return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.SetLastError(0)
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            kernel32.CloseHandle(h)
            return True
        err = kernel32.GetLastError()
        if err == 5:
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def clear_stale_lock(lock_path: str) -> None:
    """
    ロックファイルだけ残りプロセスが死んでいる場合に削除する。
    二重起動防止のロジックは変えず、起動前の救済のみ。
    """
    if not os.path.isfile(lock_path):
        return
    pid = _read_lock_pid(lock_path)
    if pid is not None and _pid_exists(pid):
        return
    try:
        os.remove(lock_path)
        print(f"[{now_iso()}] removed_stale_lock path={lock_path} reason=pid_dead_or_invalid pid={pid}")
    except OSError as e:
        print(f"[{now_iso()}] stale_lock_remove_failed path={lock_path} error={e}")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def acquire_lock(lock_path: str) -> bool:
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"pid={os.getpid()}\n")
        f.write(f"started_at={now_iso()}\n")
    return True


def release_lock(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def run_once(args: argparse.Namespace) -> int:
    cmd = [
        sys.executable,
        os.path.join(os.getcwd(), "drive_auto_run_once.py"),
        "--credentials",
        args.credentials,
        "--token",
        args.token,
        "--folder-id",
        args.folder_id,
        "--state",
        args.state,
        "--download-dir",
        args.download_dir,
        "--docs-chunk-size",
        str(args.docs_chunk_size),
    ]
    if args.max_chunks is not None:
        cmd.extend(["--max-chunks", str(args.max_chunks)])
    if args.update_state:
        cmd.append("--update-state")

    print(f"[{now_iso()}] run_once_start")
    print(f"[{now_iso()}] cmd={' '.join(cmd)}")
    completed = subprocess.run(cmd, capture_output=True, text=True)

    if completed.stdout.strip():
        print(f"[{now_iso()}] run_once_stdout")
        print(completed.stdout.rstrip())
    if completed.stderr.strip():
        print(f"[{now_iso()}] run_once_stderr")
        print(completed.stderr.rstrip())

    print(f"[{now_iso()}] run_once_exit_code={completed.returncode}")
    return completed.returncode


def main() -> None:
    load_dotenv_local()
    parser = argparse.ArgumentParser(
        description="Drive監視を常駐運用するために drive_auto_run_once.py を定期実行する"
    )
    parser.add_argument("--credentials", default="credentials.json", help="OAuthクライアントJSON")
    parser.add_argument(
        "--token",
        default="token_drive.json",
        help="Drive用OAuthトークン保存先/読込先（デフォルト: token_drive.json）",
    )
    parser.add_argument(
        "--folder-id",
        default=None,
        help="監視対象のGoogle DriveフォルダID（未指定時は環境変数 DRIVE_FOLDER_ID）",
    )
    parser.add_argument(
        "--state",
        default=os.path.join("data", "last_seen_file_ids.json"),
        help="既知file_id保存先（デフォルト: data/last_seen_file_ids.json）",
    )
    parser.add_argument(
        "--download-dir",
        default=os.path.join("data", "incoming_audio"),
        help="Driveファイルのダウンロード先",
    )
    parser.add_argument(
        "--docs-chunk-size",
        type=int,
        default=5000,
        help="run_job_once.py へ渡す Docs 分割サイズ",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="run_job_once.py へ渡す max-chunks（検証用）",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=120,
        help="ポーリング間隔（秒）。デフォルト: 120",
    )
    parser.add_argument(
        "--update-state",
        action="store_true",
        help="各ループで --update-state を付与する（推奨）",
    )
    parser.add_argument(
        "--lock-file",
        default=os.path.join("logs", "drive_auto_run_forever.lock"),
        help="常駐ランナーの二重起動防止ロック（デフォルト: logs/drive_auto_run_forever.lock）",
    )
    parser.add_argument(
        "--max-backoff-sec",
        type=int,
        default=900,
        help="失敗時バックオフの上限秒（デフォルト: 900）",
    )
    args = parser.parse_args()

    folder_id = (args.folder_id or os.getenv("DRIVE_FOLDER_ID", "").strip())
    if not folder_id:
        raise SystemExit(
            "監視フォルダが未指定です。--folder-id または環境変数 DRIVE_FOLDER_ID を設定してください。"
        )
    args.folder_id = folder_id

    if args.interval_sec < 5:
        raise ValueError("--interval-sec は 5 以上を指定してください。")
    if args.max_backoff_sec < args.interval_sec:
        raise ValueError("--max-backoff-sec は --interval-sec 以上を指定してください。")

    clear_stale_lock(args.lock_file)

    if not acquire_lock(args.lock_file):
        print(f"[{now_iso()}] already_running lock_file={args.lock_file}")
        print("status=skipped_locked")
        return

    try:
        print(f"[{now_iso()}] drive_auto_run_forever_start interval_sec={args.interval_sec}")
        print(f"[{now_iso()}] stop_with=Ctrl+C")
        consecutive_failures = 0
        while True:
            try:
                exit_code = run_once(args)
                if exit_code == 0:
                    consecutive_failures = 0
                    sleep_sec = args.interval_sec
                else:
                    consecutive_failures += 1
                    sleep_sec = min(
                        args.interval_sec * (2 ** min(consecutive_failures - 1, 4)),
                        args.max_backoff_sec,
                    )
                    print(
                        f"[{now_iso()}] run_once_failed "
                        f"consecutive_failures={consecutive_failures} next_sleep_sec={sleep_sec}"
                    )
            except Exception as e:  # noqa: BLE001
                consecutive_failures += 1
                sleep_sec = min(
                    args.interval_sec * (2 ** min(consecutive_failures - 1, 4)),
                    args.max_backoff_sec,
                )
                print(f"[{now_iso()}] run_once_exception={e}")
                print(
                    f"[{now_iso()}] exception_backoff "
                    f"consecutive_failures={consecutive_failures} next_sleep_sec={sleep_sec}"
                )
            time.sleep(sleep_sec)
    finally:
        release_lock(args.lock_file)


if __name__ == "__main__":
    main()
