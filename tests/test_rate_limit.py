import pytest

from app import rate_limit as rate_limit_module
from app.config import RATE_LIMIT_PER_MINUTE

HAPPY_DIFF = (
    "--- /dev/null\n"
    "+++ b/src/app.js\n"
    "@@ -0,0 +1,4 @@\n"
    "+eval(userInput);\n"
    "+console.log('debug');\n"
    "+// TODO: remove this\n"
    "+const ok = 1;\n"
)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limit_module.reset()
    yield
    rate_limit_module.reset()


def _post(client, auth_headers):
    return client.post("/v1/reviews", json={"diff": HAPPY_DIFF}, headers=auth_headers)


def test_burst_up_to_limit_all_succeed(client, auth_headers):
    for _ in range(RATE_LIMIT_PER_MINUTE):
        resp = _post(client, auth_headers)
        assert resp.status_code == 202


def test_request_over_limit_returns_429_with_retry_after(client, auth_headers):
    for _ in range(RATE_LIMIT_PER_MINUTE):
        assert _post(client, auth_headers).status_code == 202

    resp = _post(client, auth_headers)

    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    retry_after = int(resp.headers["Retry-After"])
    assert retry_after > 0

    body = resp.json()
    assert body["error"]["code"] == "rate_limited"
    assert "message" in body["error"]


def test_gets_and_other_endpoints_unaffected_by_post_rate_limit(client, auth_headers):
    submit_resp = _post(client, auth_headers)
    job_id = submit_resp.json()["jobId"]

    for _ in range(RATE_LIMIT_PER_MINUTE):
        _post(client, auth_headers)
    tripped = _post(client, auth_headers)
    assert tripped.status_code == 429

    get_job = client.get(f"/v1/reviews/{job_id}", headers=auth_headers)
    assert get_job.status_code == 200

    stream_resp = client.get(f"/v1/reviews/{job_id}/stream", headers=auth_headers)
    assert stream_resp.status_code == 200

    spec_resp = client.get("/spec")
    assert spec_resp.status_code == 200

    health_resp = client.get("/health")
    assert health_resp.status_code == 200


def test_new_post_succeeds_after_window_elapses(client, auth_headers, monkeypatch):
    fake_time = {"t": 10_000.0}
    monkeypatch.setattr(rate_limit_module, "_clock", lambda: fake_time["t"])

    for _ in range(RATE_LIMIT_PER_MINUTE):
        assert _post(client, auth_headers).status_code == 202

    blocked = _post(client, auth_headers)
    assert blocked.status_code == 429

    fake_time["t"] += rate_limit_module.WINDOW_SECONDS + 1

    resp = _post(client, auth_headers)
    assert resp.status_code == 202
