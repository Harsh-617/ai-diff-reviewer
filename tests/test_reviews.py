import time
import uuid

HAPPY_DIFF = (
    "--- /dev/null\n"
    "+++ b/src/app.js\n"
    "@@ -0,0 +1,4 @@\n"
    "+eval(userInput);\n"
    "+console.log('debug');\n"
    "+// TODO: remove this\n"
    "+const ok = 1;\n"
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


def test_reviews_requires_auth(client):
    resp = client.post("/v1/reviews", json={"diff": HAPPY_DIFF})
    assert resp.status_code == 401


def test_happy_path_submit_poll_findings(client, auth_headers):
    resp = client.post("/v1/reviews", json={"diff": HAPPY_DIFF}, headers=auth_headers)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    job_id = body["jobId"]

    result = _poll_until_done(client, auth_headers, job_id)
    assert result["status"] == "done"

    findings = result["findings"]
    assert [f["ruleId"] for f in findings] == ["MOCK-001", "MOCK-007", "MOCK-008"]
    assert [f["line"] for f in findings] == [1, 2, 3]
    assert findings[0]["path"] == "src/app.js"
    assert findings[0]["evidence"] == "eval(userInput);"
    assert findings[0]["id"] == "MOCK-001:src/app.js:1"

    usage = result["usage"]
    assert usage["inputBytes"] == len(HAPPY_DIFF.encode("utf-8"))
    assert usage["chunks"] == 1
    assert usage["cacheHit"] is False


def test_payload_too_large_returns_413(client, auth_headers):
    huge_body = b'{"diff": "' + b"x" * (1_048_576 + 100) + b'"}'
    resp = client.post(
        "/v1/reviews",
        content=huge_body,
        headers={**auth_headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


def test_invalid_json_returns_400(client, auth_headers):
    resp = client.post(
        "/v1/reviews",
        content=b"{not valid json",
        headers={**auth_headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_json"


def test_missing_diff_field_returns_422(client, auth_headers):
    resp = client.post("/v1/reviews", json={"options": {"provider": "mock"}}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_diff"


def test_empty_diff_string_returns_422(client, auth_headers):
    resp = client.post("/v1/reviews", json={"diff": "   "}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_diff"


def test_non_string_diff_returns_422(client, auth_headers):
    resp = client.post("/v1/reviews", json={"diff": 12345}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_diff"


def test_unparseable_diff_returns_422(client, auth_headers):
    resp = client.post("/v1/reviews", json={"diff": "this is not a unified diff"}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_diff"


def test_unknown_fields_are_ignored(client, auth_headers):
    resp = client.post(
        "/v1/reviews",
        json={"diff": HAPPY_DIFF, "unexpectedField": "whatever"},
        headers=auth_headers,
    )
    assert resp.status_code == 202


def test_idempotency_same_key_same_body_returns_same_job_id(client, auth_headers):
    headers = {**auth_headers, "Idempotency-Key": "key-1"}
    first = client.post("/v1/reviews", json={"diff": HAPPY_DIFF}, headers=headers)
    second = client.post("/v1/reviews", json={"diff": HAPPY_DIFF}, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["jobId"] == second.json()["jobId"]


def test_idempotency_same_key_different_body_returns_409(client, auth_headers):
    headers = {**auth_headers, "Idempotency-Key": "key-2"}
    first = client.post("/v1/reviews", json={"diff": HAPPY_DIFF}, headers=headers)
    assert first.status_code == 202

    other_diff = (
        "--- /dev/null\n"
        "+++ b/other.js\n"
        "@@ -0,0 +1,1 @@\n"
        "+console.log('different');\n"
    )
    second = client.post("/v1/reviews", json={"diff": other_diff}, headers=headers)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def test_caching_returns_cache_hit_with_identical_findings(client, auth_headers):
    first = client.post("/v1/reviews", json={"diff": HAPPY_DIFF}, headers=auth_headers)
    first_job_id = first.json()["jobId"]
    first_result = _poll_until_done(client, auth_headers, first_job_id)

    second = client.post("/v1/reviews", json={"diff": HAPPY_DIFF}, headers=auth_headers)
    assert second.status_code == 202
    second_job_id = second.json()["jobId"]
    assert second_job_id != first_job_id

    second_result = _poll_until_done(client, auth_headers, second_job_id)
    assert second_result["usage"]["cacheHit"] is True
    assert second_result["findings"] == first_result["findings"]


def test_unknown_job_id_returns_404(client, auth_headers):
    resp = client.get(f"/v1/reviews/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_max_findings_truncates_list_but_not_usage(client, auth_headers):
    resp = client.post(
        "/v1/reviews",
        json={"diff": HAPPY_DIFF, "options": {"maxFindings": 1}},
        headers=auth_headers,
    )
    job_id = resp.json()["jobId"]
    result = _poll_until_done(client, auth_headers, job_id)

    assert len(result["findings"]) == 1
    assert result["usage"]["inputBytes"] == len(HAPPY_DIFF.encode("utf-8"))
    assert result["usage"]["chunks"] == 1
