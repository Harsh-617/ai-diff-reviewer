import time

import pytest

from app.config import MAX_CONCURRENT_JOBS
from app.routes.v1 import reviews as reviews_module

EXPECTED_RULE_IDS = ["MOCK-001", "MOCK-007", "MOCK-008"]
EXPECTED_LINES = [1, 2, 3]

NUM_JOBS = 6
TEST_PROCESSING_DELAY_SECONDS = 0.3
SAMPLE_INTERVAL_SECONDS = 0.005
SAMPLE_TIMEOUT_SECONDS = 15.0


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
    # Sampling concurrency via HTTP polling has inherent timing jitter
    # (network round-trip, event loop scheduling, GIL handoff) no matter how
    # long the artificial per-job delay is: a poll loop that checks 6 jobs
    # one HTTP request at a time can catch job A "still running" from an
    # earlier read in the same sweep while job B has *just* started, and
    # over-count instantaneous concurrency that never actually existed at
    # any single instant. That made this test genuinely flaky (confirmed:
    # it intermittently failed even on an untouched copy of this code).
    #
    # Instead we read the in-process `_running_count` counter directly
    # (app/routes/v1/reviews.py), which is incremented/decremented exactly
    # around the semaphore-guarded window each job runs in. No HTTP
    # round-trip, no sampling gap -- this measures true concurrency.
    assert reviews_module.current_running_count() == 0, "counter should start at zero"

    job_ids = []
    for i in range(NUM_JOBS):
        resp = client.post("/v1/reviews", json={"diff": _make_diff(i)}, headers=auth_headers)
        assert resp.status_code == 202
        job_ids.append(resp.json()["jobId"])

    assert len(set(job_ids)) == NUM_JOBS, "expected distinct job ids, jobs got deduped/collided"

    peak_observed = 0
    samples = []
    ever_started = False
    deadline = time.monotonic() + SAMPLE_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        current = reviews_module.current_running_count()
        samples.append(current)
        peak_observed = max(peak_observed, current)
        if current > 0:
            ever_started = True
        if ever_started and current == 0:
            break
        time.sleep(SAMPLE_INTERVAL_SECONDS)
    else:
        raise AssertionError(
            f"timed out waiting for in-flight jobs to drain; samples tail={samples[-20:]}"
        )

    assert ever_started, "never observed any job enter the running window"

    # The semaphore must let concurrency reach the configured cap exactly,
    # and must never let it exceed that cap -- this is the actual
    # correctness check for gap 2 (a bug here means the semaphore isn't
    # bounding concurrency).
    assert peak_observed == MAX_CONCURRENT_JOBS, (
        f"expected peak concurrency to reach exactly {MAX_CONCURRENT_JOBS} "
        f"(6 jobs > {MAX_CONCURRENT_JOBS}-worker cap should saturate it), "
        f"observed peak={peak_observed}; samples={samples}"
    )
    assert all(s <= MAX_CONCURRENT_JOBS for s in samples), (
        f"observed concurrency exceeded {MAX_CONCURRENT_JOBS} at some point: samples={samples}"
    )

    print(f"\n[concurrency] in-process running-count samples: {len(samples)} taken, "
          f"peak={peak_observed} (limit={MAX_CONCURRENT_JOBS})")

    # Functional correctness: every job actually reached "done", and with
    # the right findings -- not just that concurrency was bounded.
    for i, jid in enumerate(job_ids):
        resp = client.get(f"/v1/reviews/{jid}", headers=auth_headers)
        body = resp.json()
        assert body["status"] == "done", f"job {jid} (index {i}) ended in status={body['status']!r}"

        findings = body["findings"]
        assert [f["ruleId"] for f in findings] == EXPECTED_RULE_IDS, (
            f"job {jid} (index {i}) has unexpected findings: {findings}"
        )
        assert [f["line"] for f in findings] == EXPECTED_LINES
        assert all(f["path"] == f"src/file_{i}.js" for f in findings), (
            f"job {jid} (index {i}) findings reference the wrong file -- "
            f"possible cross-job data contamination: {findings}"
        )
