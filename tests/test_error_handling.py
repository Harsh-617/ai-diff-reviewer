from fastapi.testclient import TestClient

from app.main import app


def test_unhandled_exception_returns_internal_error_envelope(client, auth_headers, monkeypatch):
    import app.routes.v1.reviews as reviews_module

    def _boom(*args, **kwargs):
        raise RuntimeError("boom: simulated unexpected failure")

    monkeypatch.setattr(reviews_module, "_normalize_options", _boom)

    # The shared `client` fixture already ran startup (db init) against the
    # per-test database; reuse the same app but disable exception re-raising
    # so we can observe the actual HTTP response our handler produces,
    # matching what a real deployment (uvicorn) would send to a client.
    with TestClient(app, raise_server_exceptions=False) as non_raising_client:
        resp = non_raising_client.post(
            "/v1/reviews",
            json={"diff": "--- /dev/null\n+++ b/a.js\n@@ -0,0 +1,1 @@\n+x\n"},
            headers=auth_headers,
        )

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "internal"
    assert "message" in body["error"]
    # Must never leak internals like the exception message or a traceback.
    assert "boom" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "Traceback" not in resp.text
