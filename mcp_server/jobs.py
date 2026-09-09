"""Background jobs. get_job_status always returns progress, never a bare error."""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "data" / "jobs"


@dataclass
class Job:
    id: str
    kind: str
    status: str  # queued | running | completed | deferred | failed | unknown
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["progress"] = self.result
        return payload


_lock = threading.Lock()
_jobs: dict[str, Job] = {}


def _path(job_id: str) -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return JOBS_DIR / f"{job_id}.json"


def _persist(job: Job) -> None:
    _path(job.id).write_text(
        json.dumps(job.to_public(), indent=2, default=str), encoding="utf-8"
    )


def get_job(job_id: str) -> Job:
    """Never raise. Unknown ids come back as status=unknown with last progress."""
    raw = (job_id or "").strip()
    if not raw:
        return Job(
            id="",
            kind="unknown",
            status="unknown",
            created_at=time.time(),
            result={"message": "job_id is required"},
        )
    with _lock:
        if raw in _jobs:
            return _jobs[raw]
    path = _path(raw)
    if not path.exists():
        return Job(
            id=raw,
            kind="unknown",
            status="unknown",
            created_at=time.time(),
            result={"message": f"No job recorded for {raw}. Last known progress: none."},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("progress", None)
        job = Job(**data)
    except Exception as exc:  # noqa: BLE001
        return Job(
            id=raw,
            kind="unknown",
            status="unknown",
            created_at=time.time(),
            result={"message": f"Could not read job file: {type(exc).__name__}"},
        )
    with _lock:
        _jobs[job.id] = job
    return job


def list_jobs(limit: int = 20) -> list[Job]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[Job] = []
    for path in files[: max(1, min(int(limit or 20), 100))]:
        job = get_job(path.stem)
        if job.status != "unknown" or job.kind != "unknown":
            out.append(job)
    return out


def update_job_progress(job_id: str, snapshot: dict[str, Any]) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            job = get_job(job_id)
            if job.status == "unknown" and job.kind == "unknown":
                return
        job.result = dict(snapshot)
        if snapshot.get("status") in {"running", "deferred", "completed"}:
            # Keep runner status unless the worker already finished.
            if job.status == "running":
                pass
    _persist(job)


def start_job(
    kind: str,
    fn: Callable[[Job], dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> Job:
    job = Job(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        status="queued",
        created_at=time.time(),
        meta=meta or {},
        result={"message": "queued"},
    )
    with _lock:
        _jobs[job.id] = job
    _persist(job)

    def worker() -> None:
        job.status = "running"
        job.started_at = time.time()
        job.result = {"message": "running", "progress": {}}
        _persist(job)
        try:
            job.result = fn(job) or {}
            status = str(job.result.get("status") or "completed")
            job.status = status if status in {"completed", "deferred"} else "completed"
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.result = {
                "status": "failed",
                "message": job.error,
                "traceback": traceback.format_exc()[-4000:],
            }
        finally:
            job.finished_at = time.time()
            _persist(job)

    threading.Thread(target=worker, name=f"pw-job-{job.id}", daemon=True).start()
    return job
