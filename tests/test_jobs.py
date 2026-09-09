import time

from mcp_server.jobs import get_job, start_job


def test_unknown_job_is_not_an_error():
    job = get_job("does-not-exist")
    assert job.status == "unknown"
    public = job.to_public()
    assert "progress" in public or job.result.get("message")
    assert "counter" in public
    assert "done" in public["counter"]
    assert public["counter"]["phase"] == "unknown"


def test_empty_job_id_unknown():
    job = get_job("")
    assert job.status == "unknown"


def test_get_job_status_never_raises_on_junk():
    job = get_job("!!!not-a-job!!!")
    public = job.to_public()
    assert public["status"] == "unknown"
    assert public["progress"] is not None
    assert public["counter"]["done"] == 0


def test_running_job_exposes_progress(tmp_path, monkeypatch):
    from mcp_server import jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)

    def fn(job):
        jobs_mod.update_job_progress(
            job.id,
            {
                "status": "running",
                "input_rows": 10,
                "progress": {"companies": 2, "title_matched": 3},
                "counter": {
                    "done": 2,
                    "total": 10,
                    "remaining": 8,
                    "pct": 20.0,
                    "title_matched": 3,
                    "name_bank": 0,
                    "companies_with_people": 1,
                    "companies_unresolved": 1,
                    "phase": "running",
                    "message": "running: 2/10 companies (20.0%)",
                },
            },
        )
        return {
            "status": "completed",
            "input_rows": 10,
            "counts": {"companies": 2, "title_matched": 3},
            "counter": {
                "done": 2,
                "total": 10,
                "remaining": 8,
                "pct": 20.0,
                "title_matched": 3,
                "name_bank": 0,
                "companies_with_people": 1,
                "companies_unresolved": 0,
                "phase": "completed",
                "message": "completed: 2/10 companies (20.0%)",
            },
        }

    started = start_job("resolve_people", fn, meta={"input_rows": 10})
    public = started.to_public()
    assert public["id"]
    assert public["status"] in {"queued", "running", "completed"}
    assert public["counter"]["total"] == 10
    deadline = time.time() + 2
    while started.status in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.05)
        started = get_job(started.id)
    done = started.to_public()
    assert done["counter"]["done"] == 2
    assert done["counter"]["total"] == 10
    assert "2/10" in done["counter"]["message"]
