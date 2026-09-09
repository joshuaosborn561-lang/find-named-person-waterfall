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
        payload["counter"] = _extract_counter(self)
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
    _persist_remote(job)


def _persist_remote(job: Job) -> None:
    """Best-effort snapshot so get_job_status works across MCP sessions."""
    if not job.id:
        return
    try:
        from people_waterfall import supabase_sync

        supabase_sync.rest_upsert(
            "pw_jobs",
            [
                {
                    "id": job.id,
                    "kind": job.kind,
                    "status": job.status,
                    "payload": job.to_public(),
                }
            ],
            on_conflict="id",
        )
    except Exception:
        return


def _extract_counter(job: Job) -> dict[str, Any]:
    from people_waterfall.progress import build_counter

    blob = job.result if isinstance(job.result, dict) else {}
    existing = blob.get("counter")
    if isinstance(existing, dict) and "done" in existing:
        return existing
    stats = blob.get("progress") if isinstance(blob.get("progress"), dict) else None
    counts = blob.get("counts") if isinstance(blob.get("counts"), dict) else None
    base = counts or stats or blob
    total = blob.get("input_rows")
    if total is None and isinstance(base, dict):
        total = base.get("input_rows")
    try:
        total_n = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_n = None
    done = 0
    title_matched = 0
    name_bank = 0
    with_people = 0
    unresolved = 0
    if isinstance(base, dict):
        done = int(base.get("companies") or base.get("done") or 0)
        title_matched = int(base.get("title_matched") or 0)
        name_bank = int(base.get("name_bank") or 0)
        with_people = int(
            base.get("companies_with_people")
            or ((base.get("resolved") or 0) + (base.get("partial") or 0))
        )
        unresolved = int(base.get("people_unresolved") or base.get("companies_unresolved") or 0)
    phase = job.status if job.status in {"queued", "running", "completed", "failed", "deferred"} else "running"
    if job.status == "queued":
        done = 0
        phase = "queued"
    return build_counter(
        done=done,
        total=total_n,
        title_matched=title_matched,
        name_bank=name_bank,
        companies_with_people=with_people,
        companies_unresolved=unresolved,
        phase=phase,
    )


def _job_from_payload(data: dict[str, Any], fallback_id: str = "") -> Job | None:
    raw = dict(data)
    raw.pop("progress", None)
    raw.pop("counter", None)
    if "id" not in raw and fallback_id:
        raw["id"] = fallback_id
    raw.setdefault("kind", "unknown")
    raw.setdefault("status", "unknown")
    raw.setdefault("created_at", time.time())
    raw.setdefault("result", {})
    raw.setdefault("meta", {})
    try:
        return Job(**{k: raw[k] for k in Job.__dataclass_fields__ if k in raw})
    except Exception:
        return None


def _load_remote(job_id: str) -> Job | None:
    try:
        from people_waterfall import supabase_sync

        rows = supabase_sync.rest_select(
            "pw_jobs",
            params={"id": f"eq.{job_id}", "select": "id,kind,status,payload", "limit": "1"},
        )
    except Exception:
        return None
    if not rows:
        return None
    payload = rows[0].get("payload")
    if isinstance(payload, dict):
        job = _job_from_payload(payload, fallback_id=job_id)
        if job:
            return job
    return Job(
        id=str(rows[0].get("id") or job_id),
        kind=str(rows[0].get("kind") or "unknown"),
        status=str(rows[0].get("status") or "unknown"),
        created_at=time.time(),
        result={"message": "Remote snapshot had no payload."},
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
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            job = _job_from_payload(data, fallback_id=raw)
            if job is None:
                raise ValueError("invalid job file")
        except Exception as exc:  # noqa: BLE001
            return Job(
                id=raw,
                kind="unknown",
                status="unknown",
                created_at=time.time(),
                result={"message": f"Could not read job file: {type(exc).__name__}"},
            )
    else:
        remote = _load_remote(raw)
        if remote is None:
            return Job(
                id=raw,
                kind="unknown",
                status="unknown",
                created_at=time.time(),
                result={"message": f"No job recorded for {raw}. Last known progress: none."},
            )
        job = remote
    with _lock:
        _jobs[job.id] = job
    return job


def list_jobs(limit: int = 20) -> list[Job]:
    cap = max(1, min(int(limit or 20), 100))
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[Job] = []
    seen: set[str] = set()
    for path in files[:cap]:
        job = get_job(path.stem)
        if job.status != "unknown" or job.kind != "unknown":
            out.append(job)
            seen.add(job.id)
    if len(out) >= cap:
        return out[:cap]
    try:
        from people_waterfall import supabase_sync

        rows = supabase_sync.rest_select(
            "pw_jobs",
            params={
                "select": "id,kind,status,payload,updated_at",
                "order": "updated_at.desc",
                "limit": str(cap),
            },
        )
    except Exception:
        rows = []
    for row in rows:
        jid = str(row.get("id") or "")
        if not jid or jid in seen:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        job = _job_from_payload(payload, fallback_id=jid) if payload else None
        if job is None:
            continue
        out.append(job)
        seen.add(jid)
        if len(out) >= cap:
            break
    return out[:cap]


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
    meta = meta or {}
    total = meta.get("input_rows")
    try:
        total_n = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_n = None
    from people_waterfall.progress import build_counter

    job = Job(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        status="queued",
        created_at=time.time(),
        meta=meta,
        result={
            "message": "queued",
            "input_rows": total_n,
            "counter": build_counter(done=0, total=total_n, phase="queued"),
        },
    )
    with _lock:
        _jobs[job.id] = job
    _persist(job)

    def worker() -> None:
        job.status = "running"
        job.started_at = time.time()
        from people_waterfall.progress import build_counter

        total = job.meta.get("input_rows") if isinstance(job.meta, dict) else None
        try:
            total_n = int(total) if total is not None else None
        except (TypeError, ValueError):
            total_n = None
        job.result = {
            "message": "running",
            "input_rows": total_n,
            "progress": {},
            "counter": build_counter(done=0, total=total_n, phase="running"),
        }
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
                "input_rows": total_n,
                "traceback": traceback.format_exc()[-4000:],
                "counter": build_counter(done=0, total=total_n, phase="failed"),
            }
        finally:
            job.finished_at = time.time()
            _persist(job)

    threading.Thread(target=worker, name=f"pw-job-{job.id}", daemon=True).start()
    return job
