import time

HAPPY_DIFF = (
    "--- /dev/null\n"
    "+++ b/src/app.js\n"
    "@@ -0,0 +1,1 @@\n"
    "+console.log('debug');\n"
)


def _poll_until_done(client, headers, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    body = None
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/reviews/{job_id}", headers=headers)
        body = resp.json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish in time, last body={body}")


def test_unknown_provider_value_defaults_silently_to_mock(client, auth_headers):
    resp = client.post(
        "/v1/reviews",
        json={"diff": HAPPY_DIFF, "options": {"provider": "banana"}},
        headers=auth_headers,
    )

    # Must not crash the request itself -- job is accepted like any other.
    assert resp.status_code == 202
    job_id = resp.json()["jobId"]

    # Must not hang or crash the worker -- job reaches a terminal state
    # well within the normal processing window.
    result = _poll_until_done(client, auth_headers, job_id, timeout=5.0)
    assert result["status"] == "done"

    # Falls back to the mock rule engine (never attempts an LLM call for an
    # unrecognized provider), so findings match the standard mock output.
    assert [f["ruleId"] for f in result["findings"]] == ["MOCK-007"]
