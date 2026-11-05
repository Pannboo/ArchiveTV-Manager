"""Background jobs run under APScheduler: they fire, report status, never
overlap, and a failing job doesn't stop the others."""

import asyncio

from archivetv import jobs


def test_scheduler_runs_jobs_and_survives_failures(monkeypatch):
    calls = {"ok": 0, "slow": 0}

    async def ok():
        calls["ok"] += 1

    async def slow():
        calls["slow"] += 1
        await asyncio.sleep(2.5)  # longer than its interval: must not overlap

    def boom():
        raise RuntimeError("job failure")

    monkeypatch.setattr(jobs, "JOB_SPECS", [("ok", 1, ok, 0), ("slow", 1, slow, 0), ("boom", 1, boom, 0)])
    monkeypatch.setattr(jobs, "last_run", {})

    async def run():
        jobs.start_jobs()
        await asyncio.sleep(2.2)
        status = jobs.job_status()
        jobs.stop_jobs()
        return status

    status = asyncio.run(run())
    assert calls["ok"] >= 2 and calls["slow"] == 1
    assert status["ok"]["last_run"] and status["ok"]["next_run"]
    assert status["boom"]["last_run"] is None  # failures are logged, not recorded as runs
    assert jobs.scheduler is None
