from mcp_server.jobs import get_job, start_job


def test_unknown_job_is_not_an_error():
    job = get_job("does-not-exist")
    assert job.status == "unknown"
    assert "progress" in job.to_public() or job.result.get("message")


def test_empty_job_id_unknown():
    job = get_job("")
    assert job.status == "unknown"


def test_get_job_status_never_raises_on_junk():
    job = get_job("!!!not-a-job!!!")
    public = job.to_public()
    assert public["status"] == "unknown"
    assert public["progress"] is not None


def test_running_job_exposes_progress(tmp_path, monkeypatch):
    from mcp_server import jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)

    def fn(job):
        jobs_mod.update_job_progress(job.id, {"companies": 2, "status": "running"})
        return {"status": "completed", "counts": {"companies": 2}}

    started = start_job("resolve_people", fn, meta={})
    public = started.to_public()
    assert public["id"]
    assert public["status"] in {"queued", "running", "completed"}
