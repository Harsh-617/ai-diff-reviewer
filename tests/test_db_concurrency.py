import time

import pytest

from app.config import MAX_CONCURRENT_JOBS
from app.db import get_connection
from app.routes.v1 import reviews as reviews_module

NUM_JOBS = 8
TEST_PROCESSING_DELAY_SECONDS = 0.3


@pytest.fixture(autouse=True)
def _widen_processing_window(monkeypatch):
    # Force jobs to stay "running" long enough that, with NUM_JOBS > the
    # 4-worker cap, later batches' writes land while earlier batches' writes
    # (and the WAL checkpoint machinery) are still active on other threads.
    monkeypatch.setattr(
        reviews_module, "_TEST_PROCESSING_DELAY_SECONDS", TEST_PROCESSING_DELAY_SECONDS
    )


def _make_diff(i: int) -> str:
    return (
        "--- /dev/null\n"
        f"+++ b/src/file_{i}.js\n"
        "@@ -0,0 +1,2 @@\n"
        f"+console.log('debug-{i}');\n"
        f"+const ok = {i};\n"
    )


def test_new_connections_have_wal_and_busy_timeout(client):
    conn = get_connection()
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 5000


def test_eight_concurrent_jobs_survive_overlapping_writes(client, auth_headers):
    job_ids = []
    for i in range(NUM_JOBS):
        resp = client.post("/v1/reviews", json={"diff": _make_diff(i)}, headers=auth_headers)
        assert resp.status_code == 202, f"submission {i} failed: {resp.status_code} {resp.text}"
        job_ids.append(resp.json()["jobId"])

    assert len(set(job_ids)) == NUM_JOBS, "expected distinct job ids, jobs got deduped/collided"

    statuses = {jid: "queued" for jid in job_ids}
    deadline = time.monotonic() + 30.0

    while time.monotonic() < deadline and any(
        statuses[jid] not in ("done", "failed") for jid in job_ids
    ):
        for jid in job_ids:
            if statuses[jid] in ("done", "failed"):
                continue
            resp = client.get(f"/v1/reviews/{jid}", headers=auth_headers)
            statuses[jid] = resp.json()["status"]

    for i, jid in enumerate(job_ids):
        assert statuses[jid] in ("done", "failed"), (
            f"job {jid} (index {i}) did not finish in time, last status={statuses[jid]!r}"
        )

    # None of the NUM_JOBS jobs (more than MAX_CONCURRENT_JOBS={MAX_CONCURRENT_JOBS}
    # workers) should have failed due to SQLite lock contention -- that's exactly
    # what WAL mode + busy_timeout exist to prevent.
    for i, jid in enumerate(job_ids):
        resp = client.get(f"/v1/reviews/{jid}", headers=auth_headers)
        body = resp.json()
        if body["status"] == "failed":
            error = body.get("error", "")
            assert "database is locked" not in (error or "").lower(), (
                f"job {jid} (index {i}) failed due to a database lock: {error}"
            )
            pytest.fail(f"job {jid} (index {i}) unexpectedly failed: {error}")
        assert body["status"] == "done"
