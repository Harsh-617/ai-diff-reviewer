import json
import time
from types import SimpleNamespace

import app.llm_provider as llm_provider_module

DIFF = (
    "--- /dev/null\n"
    "+++ b/src/app.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+x = 1\n"
    "+y = 2\n"
)


class _FakeGroqClient:
    """Stands in for AsyncGroq: same async-context-manager + chat.completions.create shape."""

    def __init__(self, content: str | None = None, raise_exc: Exception | None = None):
        self._content = content
        self._raise_exc = raise_exc
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def _create(self, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
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


def _submit_llm_job(client, headers, diff=DIFF):
    resp = client.post(
        "/v1/reviews",
        json={"diff": diff, "options": {"provider": "llm"}},
        headers=headers,
    )
    assert resp.status_code == 202
    return resp.json()["jobId"]


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


def test_llm_review_success_stores_sorted_findings(client, auth_headers, monkeypatch):
    findings_payload = [
        {
            "id": "LLM-1", "ruleId": "LLM-SEC-001", "path": "src/app.py", "line": 2,
            "severity": "high", "category": "security", "title": "second issue",
            "evidence": "y = 2",
        },
        {
            "id": "LLM-2", "ruleId": "LLM-SEC-000", "path": "src/app.py", "line": 1,
            "severity": "medium", "category": "style", "title": "first issue",
            "evidence": "x = 1",
        },
    ]
    fake_client = _FakeGroqClient(content=json.dumps(findings_payload))
    monkeypatch.setattr(llm_provider_module, "_build_client", lambda: fake_client)

    job_id = _submit_llm_job(client, auth_headers)
    body = _poll_until_done(client, auth_headers, job_id)

    assert body["status"] == "done"
    findings = body["findings"]
    # findings must come back sorted by (path, line, ruleId), same as the mock provider
    assert [f["id"] for f in findings] == ["LLM-2", "LLM-1"]
    assert [f["line"] for f in findings] == [1, 2]
    assert findings[0]["ruleId"] == "LLM-SEC-000"
    assert findings[1]["ruleId"] == "LLM-SEC-001"
    assert findings[0]["evidence"] == "x = 1"


def test_llm_review_connection_error_fails_job_and_releases_semaphore(client, auth_headers, monkeypatch):
    fake_client = _FakeGroqClient(raise_exc=ConnectionError("connection refused"))
    monkeypatch.setattr(llm_provider_module, "_build_client", lambda: fake_client)

    job_id = _submit_llm_job(client, auth_headers)
    body = _poll_until_done(client, auth_headers, job_id)

    assert body["status"] == "failed"
    assert isinstance(body.get("error"), str) and body["error"].strip() != ""

    # A subsequent, unrelated (mock-provider) job must still process normally --
    # this proves the semaphore slot used by the failed LLM job was released,
    # not left stuck.
    resp = client.post("/v1/reviews", json={"diff": DIFF}, headers=auth_headers)
    assert resp.status_code == 202
    next_job_id = resp.json()["jobId"]
    next_body = _poll_until_done(client, auth_headers, next_job_id)
    assert next_body["status"] == "done"


def test_llm_review_malformed_json_fails_job_gracefully(client, auth_headers, monkeypatch):
    fake_client = _FakeGroqClient(content="not valid json {")
    monkeypatch.setattr(llm_provider_module, "_build_client", lambda: fake_client)

    job_id = _submit_llm_job(client, auth_headers)
    body = _poll_until_done(client, auth_headers, job_id)

    assert body["status"] == "failed"
    assert isinstance(body.get("error"), str) and body["error"].strip() != ""

    # confirm the worker is still healthy after a malformed-JSON failure too
    resp = client.post("/v1/reviews", json={"diff": DIFF}, headers=auth_headers)
    assert resp.status_code == 202
    next_job_id = resp.json()["jobId"]
    next_body = _poll_until_done(client, auth_headers, next_job_id)
    assert next_body["status"] == "done"


def test_llm_review_failure_stream_terminates_live(client, auth_headers, monkeypatch):
    fake_client = _FakeGroqClient(raise_exc=ConnectionError("connection refused"))
    monkeypatch.setattr(llm_provider_module, "_build_client", lambda: fake_client)

    job_id = _submit_llm_job(client, auth_headers)

    # connect to the stream right away, before the job has necessarily failed yet
    stream_resp = client.get(f"/v1/reviews/{job_id}/stream", headers=auth_headers)
    assert stream_resp.status_code == 200

    events = _parse_sse(stream_resp.text)

    assert events[0] == ("status", {"status": "running"})
    assert events[-1] == ("status", {"status": "failed"})
    assert not any(event_type == "done" for event_type, _ in events)

    body = client.get(f"/v1/reviews/{job_id}", headers=auth_headers).json()
    assert body["status"] == "failed"


def test_llm_review_failure_stream_replay_matches_live(client, auth_headers, monkeypatch):
    fake_client = _FakeGroqClient(content="not valid json {")
    monkeypatch.setattr(llm_provider_module, "_build_client", lambda: fake_client)

    job_id = _submit_llm_job(client, auth_headers)
    body = _poll_until_done(client, auth_headers, job_id)
    assert body["status"] == "failed"

    # connect to the stream only after the job has already terminally failed --
    # this must replay the full historical sequence and still terminate, just
    # like the live case above.
    replay_resp = client.get(f"/v1/reviews/{job_id}/stream", headers=auth_headers)
    assert replay_resp.status_code == 200

    events = _parse_sse(replay_resp.text)

    assert events[0] == ("status", {"status": "running"})
    assert events[-1] == ("status", {"status": "failed"})
    assert not any(event_type == "done" for event_type, _ in events)
