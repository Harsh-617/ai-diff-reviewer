import json
import time
import uuid

STREAM_DIFF = (
    "diff --git a/b_file.js b/b_file.js\n"
    "new file mode 100644\n"
    "index 0000000..1111111\n"
    "--- /dev/null\n"
    "+++ b/b_file.js\n"
    "@@ -0,0 +1,2 @@\n"
    "+console.log('b1');\n"
    "+eval(b2);\n"
    "diff --git a/a_file.js b/a_file.js\n"
    "new file mode 100644\n"
    "index 0000000..1111111\n"
    "--- /dev/null\n"
    "+++ b/a_file.js\n"
    "@@ -0,0 +1,2 @@\n"
    "+eval(a1);\n"
    "+console.log('a2');\n"
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


def _parse_sse(text):
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event_type = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_type = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        events.append((event_type, json.loads(data)))
    return events


def _submit(client, headers, diff=STREAM_DIFF, options=None):
    payload = {"diff": diff}
    if options is not None:
        payload["options"] = options
    resp = client.post("/v1/reviews", json=payload, headers=headers)
    assert resp.status_code == 202
    return resp.json()["jobId"]


def test_stream_immediate_connect_full_sequence(client, auth_headers):
    job_id = _submit(client, auth_headers)

    stream_resp = client.get(f"/v1/reviews/{job_id}/stream", headers=auth_headers)

    assert stream_resp.status_code == 200
    assert stream_resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(stream_resp.text)

    assert events[0] == ("status", {"status": "running"})

    finding_events = events[1:-1]
    assert all(event_type == "finding" for event_type, _ in finding_events)
    findings = [data for _, data in finding_events]
    assert [f["path"] for f in findings] == ["a_file.js", "a_file.js", "b_file.js", "b_file.js"]
    assert [f["line"] for f in findings] == [1, 2, 1, 2]
    assert [f["ruleId"] for f in findings] == ["MOCK-001", "MOCK-007", "MOCK-007", "MOCK-001"]

    done_type, done_data = events[-1]
    assert done_type == "done"
    assert done_data["total"] == len(findings)
    assert done_data["usage"]["inputBytes"] == len(STREAM_DIFF.encode("utf-8"))
    assert done_data["usage"]["chunks"] == 1
    assert done_data["usage"]["cacheHit"] is False


def test_stream_replay_after_done_matches_live_stream(client, auth_headers):
    # Distinct (no-op) options on each submission keep the diff+options hash
    # different, so the second job scans fresh instead of short-circuiting
    # through the Phase 4 cache-hit replay path -- maxFindings only truncates
    # the polling GET response, not the events a job emits while it runs, so
    # the two jobs' stream output is still expected to match exactly.
    replay_job_id = _submit(client, auth_headers, options={"maxFindings": 50})
    _poll_until_done(client, auth_headers, replay_job_id)

    replay_resp = client.get(f"/v1/reviews/{replay_job_id}/stream", headers=auth_headers)
    replay_events = _parse_sse(replay_resp.text)

    live_job_id = _submit(client, auth_headers, options={"maxFindings": 60})
    live_resp = client.get(f"/v1/reviews/{live_job_id}/stream", headers=auth_headers)
    live_events = _parse_sse(live_resp.text)

    assert replay_resp.status_code == 200
    assert replay_events == live_events


def test_stream_unknown_job_id_returns_404(client, auth_headers):
    resp = client.get(f"/v1/reviews/{uuid.uuid4()}/stream", headers=auth_headers)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_stream_finding_order_matches_polling_endpoint(client, auth_headers):
    job_id = _submit(client, auth_headers)
    result = _poll_until_done(client, auth_headers, job_id)
    poll_findings = result["findings"]

    stream_resp = client.get(f"/v1/reviews/{job_id}/stream", headers=auth_headers)
    events = _parse_sse(stream_resp.text)
    stream_findings = [data for event_type, data in events if event_type == "finding"]

    assert stream_findings == poll_findings
