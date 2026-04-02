import argparse
import json
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


def _read_lock_start_ticks(lock_path: str) -> float | None:
    """ロックファイルに書かれた proc_start_ticks を読む（PID 再利用検出用）。"""
    try:
        with open(lock_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("proc_start_ticks="):
                    return float(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return None


def _get_process_start_ticks(pid: int) -> float | None:
    """Linux /proc/{pid}/stat からプロセス開始時刻（clock ticks）を取得する。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            fields = f.read().split()
        return float(fields[21])
    except (OSError, ValueError, IndexError):
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


def clear_stale_lock(lock_path: str, max_lock_age_sec: int = 1800) -> None:
    """
    ロックファイルだけ残りプロセスが死んでいる場合に削除する。
    以下の3段階で検出する:
      1. 年齢チェック: max_lock_age_sec（デフォルト30分）より古ければ無条件削除
         → コンテナ再起動後の PID 再利用による誤判定を防ぐ
      2. PID 存在チェック: PID が存在しなければ削除
      3. PID 再利用チェック: PID は存在するが /proc start_ticks が異なれば削除
         → 同じ PID を別プロセスが再利用しているケース
    """
    if not os.path.isfile(lock_path):
        return

    # 1. 年齢チェック（Railway コンテナ再起動後の残存ロック対策）
    try:
        age_sec = time.time() - os.path.getmtime(lock_path)
        if age_sec > max_lock_age_sec:
            os.remove(lock_path)
            print(
                f"[{now_iso()}] removed_stale_lock path={lock_path} "
                f"reason=too_old age_sec={int(age_sec)}"
            )
            return
    except OSError:
        pass

    pid = _read_lock_pid(lock_path)

    # 2. PID 存在チェック
    if pid is None or not _pid_exists(pid):
        try:
            os.remove(lock_path)
            print(f"[{now_iso()}] removed_stale_lock path={lock_path} reason=pid_dead pid={pid}")
        except OSError as e:
            print(f"[{now_iso()}] stale_lock_remove_failed path={lock_path} error={e}")
        return

    # 3. PID 再利用チェック（Linux のみ）
    lock_ticks = _read_lock_start_ticks(lock_path)
    if lock_ticks is not None:
        actual_ticks = _get_process_start_ticks(pid)
        if actual_ticks is not None and abs(actual_ticks - lock_ticks) > 1.0:
            try:
                os.remove(lock_path)
                print(
                    f"[{now_iso()}] removed_stale_lock path={lock_path} "
                    f"reason=pid_reuse pid={pid} "
                    f"lock_ticks={lock_ticks} actual_ticks={actual_ticks}"
                )
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
    my_pid = os.getpid()
    my_ticks = _get_process_start_ticks(my_pid)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"pid={my_pid}\n")
        f.write(f"started_at={now_iso()}\n")
        if my_ticks is not None:
            f.write(f"proc_start_ticks={my_ticks}\n")
    return True


def release_lock(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_known_ids_count(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            if isinstance(data.get("ids"), list):
                return len(data["ids"])
            return len(data)
    except Exception:
        return 0
    return 0


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def update_worker_status(status_path: str, **patch) -> dict:
    current = _read_json(status_path)
    current.update(patch)
    current["updated_at"] = _now_iso()
    _atomic_write_json(status_path, current)
    return current


def _parse_run_once_output(stdout_text: str) -> dict:
    result = {
        "status": "failed",
        "job_id": "",
        "processed_file_name": "",
    }
    for line in stdout_text.splitlines():
        s = line.strip()
        if s.startswith("status="):
            result["status"] = s.split("=", 1)[1].strip() or result["status"]
        elif s.startswith("job_id="):
            result["job_id"] = s.split("=", 1)[1].strip()
        elif s.startswith("processed_file_name="):
            result["processed_file_name"] = s.split("=", 1)[1].strip()
    return result


def run_once(args: argparse.Namespace) -> tuple[int, dict]:
    cmd = [
        sys.executable,
        os.path.join(os.getcwd(), "drive_auto_run_once.py"),
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
    parsed = _parse_run_once_output(completed.stdout or "")
    return completed.returncode, parsed


def main() -> None:
    load_dotenv_local()
    parser = argparse.ArgumentParser(
        description="Drive監視を常駐運用するために drive_auto_run_once.py を定期実行する"
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
    parser.add_argument(
        "--worker-status-path",
        default=os.path.join("data", "worker_status.json"),
        help="worker状態のJSON保存先（デフォルト: data/worker_status.json）",
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
        update_worker_status(
            args.worker_status_path,
            worker_state="idle",
            lock_state="skipped_locked",
            last_result="skipped_locked",
        )
        return

    try:
        print(f"[{now_iso()}] drive_auto_run_forever_start interval_sec={args.interval_sec}")
        print(f"[{now_iso()}] stop_with=Ctrl+C")
        update_worker_status(
            args.worker_status_path,
            worker_state="idle",
            lock_state="acquired",
            interval_sec=args.interval_sec,
            started_at=_now_iso(),
            last_result="startup",
            poll_count=0,
            consecutive_no_new_files=0,
        )
        consecutive_failures = 0
        consecutive_no_new_files = 0
        poll_count = 0
        while True:
            poll_count += 1
            update_worker_status(
                args.worker_status_path,
                worker_state="polling",
                last_poll_at=_now_iso(),
                poll_count=poll_count,
                known_ids_count=_read_known_ids_count(args.state),
            )
            try:
                update_worker_status(args.worker_status_path, worker_state="processing")
                exit_code, run_meta = run_once(args)
                run_status = str(run_meta.get("status") or "")
                run_job_id = str(run_meta.get("job_id") or "")
                if exit_code == 0 and run_status == "success":
                    consecutive_failures = 0
                    consecutive_no_new_files = 0
                    sleep_sec = args.interval_sec
                    update_worker_status(
                        args.worker_status_path,
                        worker_state="idle",
                        last_result="success",
                        last_detected_file_at=_now_iso(),
                        last_started_job_id=(run_job_id or None),
                        last_finished_job_id=(run_job_id or None),
                        last_processed_file_name=(run_meta.get("processed_file_name") or None),
                        consecutive_no_new_files=consecutive_no_new_files,
                        last_error=None,
                    )
                elif exit_code == 0 and run_status == "no_new_files":
                    consecutive_failures = 0
                    consecutive_no_new_files += 1
                    sleep_sec = args.interval_sec
                    update_worker_status(
                        args.worker_status_path,
                        worker_state="idle",
                        last_result="no_new_files",
                        consecutive_no_new_files=consecutive_no_new_files,
                        last_error=None,
                    )
                else:
                    consecutive_failures += 1
                    consecutive_no_new_files = 0
                    sleep_sec = min(
                        args.interval_sec * (2 ** min(consecutive_failures - 1, 4)),
                        args.max_backoff_sec,
                    )
                    print(
                        f"[{now_iso()}] run_once_failed "
                        f"consecutive_failures={consecutive_failures} next_sleep_sec={sleep_sec}"
                    )
                    update_worker_status(
                        args.worker_status_path,
                        worker_state="error",
                        last_result="failed",
                        consecutive_no_new_files=consecutive_no_new_files,
                        last_error=(
                            f"run_once_failed exit_code={exit_code} status={run_status} "
                            f"job_id={run_job_id}"
                        ),
                    )
            except Exception as e:  # noqa: BLE001
                consecutive_failures += 1
                consecutive_no_new_files = 0
                sleep_sec = min(
                    args.interval_sec * (2 ** min(consecutive_failures - 1, 4)),
                    args.max_backoff_sec,
                )
                print(f"[{now_iso()}] run_once_exception={e}")
                print(
                    f"[{now_iso()}] exception_backoff "
                    f"consecutive_failures={consecutive_failures} next_sleep_sec={sleep_sec}"
                )
                update_worker_status(
                    args.worker_status_path,
                    worker_state="error",
                    last_result="failed",
                    consecutive_no_new_files=consecutive_no_new_files,
                    last_error=f"exception={e!r}",
                )
            time.sleep(sleep_sec)
    finally:
        update_worker_status(
            args.worker_status_path,
            worker_state="idle",
            lock_state="released",
            stopped_at=_now_iso(),
        )
        release_lock(args.lock_file)


if __name__ == "__main__":
    main()
