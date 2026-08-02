import time

import pytest

from app.config import MAX_CONCURRENT_JOBS
from app.routes.v1 import reviews as reviews_module

EXPECTED_RULE_IDS = ["MOCK-001", "MOCK-007", "MOCK-008"]
EXPECTED_LINES = [1, 2, 3]

TEST_PROCESSING_DELAY_SECONDS = 0.3


@pytest.fixture(autouse=True)
def _widen_processing_window(monkeypatch):
    monkeypatch.setattr(
        reviews_module, "_TEST_PROCESSING_DELAY_SECONDS", TEST_PROCESSING_DELAY_SECONDS
    )


def _make_diff(i: int) -> str:
    return (
        "--- /dev/null\n"
        f"+++ b/src/file_{i}.js\n"
        "@@ -0,0 +1,4 @@\n"
        "+eval(userInput);\n"
        f"+console.log('debug-{i}');\n"
        "+// TODO: remove this\n"
        f"+const ok = {i};\n"
    )


def test_six_concurrent_jobs_complete_and_semaphore_bounds_concurrency(client, auth_headers):
    job_ids = []
    for i in range(6):
        resp = client.post("/v1/reviews", json={"diff": _make_diff(i)}, headers=auth_headers)
        assert resp.status_code == 202
        job_ids.append(resp.json()["jobId"])

    assert len(set(job_ids)) == 6, "expected 6 distinct job ids, jobs got deduped/collided"

    statuses = {jid: "queued" for jid in job_ids}
    max_observed_running = 0
    running_samples = []
    deadline = time.monotonic() + 30.0

    while time.monotonic() < deadline and any(
        statuses[jid] not in ("done", "failed") for jid in job_ids
    ):
        running_count = 0
        for jid in job_ids:
            if statuses[jid] in ("done", "failed"):
                continue
            resp = client.get(f"/v1/reviews/{jid}", headers=auth_headers)
            body = resp.json()
            statuses[jid] = body["status"]
            if body["status"] == "running":
                running_count += 1
        running_samples.append(running_count)
        max_observed_running = max(max_observed_running, running_count)

    for i, jid in enumerate(job_ids):
        assert statuses[jid] == "done", f"job {jid} (index {i}) ended in status={statuses[jid]!r}"

    # The semaphore must never allow more than MAX_CONCURRENT_JOBS jobs
    # simultaneously in "running" status -- this is the actual correctness
    # check (a bug here would mean the semaphore isn't bounding concurrency).
    assert max_observed_running <= MAX_CONCURRENT_JOBS, (
        f"observed {max_observed_running} jobs running simultaneously, "
        f"more than the configured limit of {MAX_CONCURRENT_JOBS}"
    )

    print(f"\n[concurrency] running-count samples: {running_samples}")
    print(f"[concurrency] max observed simultaneously running: {max_observed_running} "
          f"(limit={MAX_CONCURRENT_JOBS})")

    for i, jid in enumerate(job_ids):
        resp = client.get(f"/v1/reviews/{jid}", headers=auth_headers)
        body = resp.json()
        findings = body["findings"]
        assert [f["ruleId"] for f in findings] == EXPECTED_RULE_IDS, (
            f"job {jid} (index {i}) has unexpected findings: {findings}"
        )
        assert [f["line"] for f in findings] == EXPECTED_LINES
        assert all(f["path"] == f"src/file_{i}.js" for f in findings), (
            f"job {jid} (index {i}) findings reference the wrong file -- "
            f"possible cross-job data contamination: {findings}"
        )
