import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class JobProgressEvent:
    at: str
    phase: str
    status: str  # running/success/failed/skipped
    detail: dict[str, Any]


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_progress_", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp_path, path)
        except PermissionError:
            # Windows: readers may hold the file handle; replace can fail.
            # For progress tracking we prefer "never crash the pipeline" over strict atomicity.
            with open(path, "w", encoding="utf-8") as wf:
                json.dump(payload, wf, ensure_ascii=False, indent=2)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def job_progress_path(input_root: str, job_id: str) -> str:
    return os.path.join(input_root, job_id, "progress.json")


def last_progress_path() -> str:
    return os.path.join("data", "last_job_progress.json")


def read_last_job_progress() -> dict[str, Any] | None:
    p = last_progress_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def read_job_progress(input_root: str, job_id: str) -> dict[str, Any] | None:
    p = job_progress_path(input_root, job_id)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _safe_init_payload(job_id: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "overall_status": "running",
        "phase": "start",
        "updated_at": now_iso(),
        "events": [],
        # Extra top-level key (for cheap artifact existence checks)
        "artifacts": {},
    }


def update_job_progress(
    *,
    input_root: str,
    job_id: str,
    phase: str,
    status: str,
    detail: dict[str, Any] | None = None,
    overall_status: str | None = None,
) -> None:
    """
    無駄に重くしないため、毎回の更新は JSON 追記ではなく「最新状態スナップショット + events追記」。
    """
    if detail is None:
        detail = {}
    job_p = job_progress_path(input_root, job_id)
    last_p = last_progress_path()

    try:
        if os.path.isfile(job_p):
            with open(job_p, "r", encoding="utf-8") as f:
                payload = json.load(f)
        else:
            payload = _safe_init_payload(job_id)
    except Exception:
        payload = _safe_init_payload(job_id)

    evt = JobProgressEvent(at=now_iso(), phase=phase, status=status, detail=detail or {})
    events = payload.get("events")
    if not isinstance(events, list):
        events = []
    events.append(asdict(evt))
    payload["events"] = events[-200:]  # cap memory / file size
    payload["phase"] = phase
    payload["overall_status"] = overall_status or payload.get("overall_status") or "running"
    payload["updated_at"] = now_iso()

    _atomic_write_json(job_p, payload)

    # Also update last job snapshot (for quick reads)
    last_payload = {
        "job_id": job_id,
        "overall_status": payload.get("overall_status"),
        "phase": payload.get("phase"),
        "updated_at": payload.get("updated_at"),
        "detail": detail,
    }
    try:
        _atomic_write_json(last_p, last_payload)
    except Exception:
        # best-effort only
        pass


def init_job_progress(*, input_root: str, job_id: str) -> None:
    update_job_progress(
        input_root=input_root,
        job_id=job_id,
        phase="start",
        status="running",
        detail={},
        overall_status="running",
    )


def finalize_job_progress(*, input_root: str, job_id: str, overall_status: str = "success") -> None:
    update_job_progress(
        input_root=input_root,
        job_id=job_id,
        phase="done",
        status=overall_status,
        detail={},
        overall_status=overall_status,
    )


def ensure_artifact_flags(*, input_root: str, job_id: str, artifacts: dict[str, Any]) -> None:
    """
    artifacts を progress.json の artifacts に反映（存在/サイズなど）。
    """
    job_p = job_progress_path(input_root, job_id)
    try:
        if os.path.isfile(job_p):
            with open(job_p, "r", encoding="utf-8") as f:
                payload = json.load(f)
        else:
            payload = _safe_init_payload(job_id)
    except Exception:
        payload = _safe_init_payload(job_id)

    payload.setdefault("artifacts", {})
    if isinstance(payload["artifacts"], dict):
        payload["artifacts"].update(artifacts)
    payload["updated_at"] = now_iso()
    _atomic_write_json(job_p, payload)

