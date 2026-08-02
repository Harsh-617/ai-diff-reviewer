#!/usr/bin/env python3
"""
Live smoke test for a deployed ai-diff-reviewer instance.

Exercises, against a real running deployment (not the test client):
  1. GET  /health
  2. GET  /spec
  3. Auth rejection (401) on /v1/reviews without/with a bad bearer token
  4. A full mock review: submit -> poll to done -> verify findings
  5. SSE streaming of a job's status/finding/done events
  6. Idempotency-Key resubmission (same key + same body -> same jobId)
  7. Cache-hit resubmission (identical diff+options -> second run cacheHit=true)
  8. The 30/min (per /spec) rate limit boundary on POST /v1/reviews

Usage:
    python scripts/smoke_test.py --base-url https://your-app.onrender.com --token $AUTH_TOKEN
    python scripts/smoke_test.py https://your-app.onrender.com $AUTH_TOKEN

Base URL and token may also be supplied via the SMOKE_BASE_URL and
SMOKE_AUTH_TOKEN environment variables. Exits 0 if every step passes,
1 otherwise. Prints a PASS/FAIL line per step plus a final summary.

This script only reads/writes through the public HTTP API -- it does not
touch the deployment's database or filesystem directly, so it's safe to
run against a real production instance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx

REQUEST_TIMEOUT_SECONDS = 30.0
JOB_DONE_TIMEOUT_SECONDS = 30.0
JOB_POLL_INTERVAL_SECONDS = 0.5


def _mock_diff(nonce: str) -> str:
    """A diff that trips exactly MOCK-001, MOCK-007, MOCK-008 in that order."""
    return (
        "--- /dev/null\n"
        f"+++ b/src/smoke_{nonce}.js\n"
        "@@ -0,0 +1,3 @@\n"
        "+eval(userInput);\n"
        "+console.log('debug');\n"
        "+// TODO: remove this\n"
    )


@dataclass
class Results:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def ok(self, name: str) -> None:
        self.passed.append(name)
        print(f"[PASS] {name}")

    def bad(self, name: str, reason: str) -> None:
        self.failed.append(name)
        print(f"[FAIL] {name}: {reason}")


class SmokeTest:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = httpx.Client(base_url=self.base_url, timeout=REQUEST_TIMEOUT_SECONDS)
        self.results = Results()
        self.rate_limit_per_minute: int | None = None
        self.post_count = 0

    def close(self) -> None:
        self.client.close()

    def _auth_headers(self, token: str | None = None) -> dict:
        return {"Authorization": f"Bearer {token if token is not None else self.token}"}

    def _post_review(self, diff: str, options: dict | None = None, idempotency_key: str | None = None) -> httpx.Response:
        payload: dict = {"diff": diff}
        if options is not None:
            payload["options"] = options
        headers = self._auth_headers()
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        self.post_count += 1
        return self.client.post("/v1/reviews", json=payload, headers=headers)

    def _poll_until_terminal(self, job_id: str, timeout: float = JOB_DONE_TIMEOUT_SECONDS) -> dict:
        deadline = time.monotonic() + timeout
        body = None
        while time.monotonic() < deadline:
            resp = self.client.get(f"/v1/reviews/{job_id}", headers=self._auth_headers())
            resp.raise_for_status()
            body = resp.json()
            if body["status"] in ("done", "failed"):
                return body
            time.sleep(JOB_POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"job {job_id} did not reach a terminal state within {timeout}s (last={body})")

    # -- steps ------------------------------------------------------------

    def step_health(self) -> None:
        name = "health"
        try:
            resp = self.client.get("/health")
            body = resp.json()
            assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
            assert body.get("status") == "ok", f"unexpected status field: {body}"
            assert "version" in body and "uptimeSeconds" in body, f"missing fields: {body}"
            self.results.ok(name)
        except Exception as exc:
            self.results.bad(name, str(exc))

    def step_spec(self) -> None:
        name = "spec"
        try:
            resp = self.client.get("/spec")
            body = resp.json()
            assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
            assert body.get("specVersion"), f"missing specVersion: {body}"
            assert set(body.get("providers", [])) >= {"mock", "llm"}, f"unexpected providers: {body}"
            limits = body.get("limits", {})
            for key in ("maxPayloadBytes", "chunkBytes", "maxConcurrentJobs", "rateLimitPerMinute"):
                assert key in limits, f"missing limits.{key}: {body}"
            self.rate_limit_per_minute = int(limits["rateLimitPerMinute"])
            self.results.ok(name)
        except Exception as exc:
            self.results.bad(name, str(exc))

    def step_auth_rejection(self) -> None:
        name = "auth_rejection"
        try:
            no_header = self.client.post("/v1/reviews", json={"diff": _mock_diff("auth")})
            assert no_header.status_code == 401, f"missing-header request got {no_header.status_code}, want 401"
            assert no_header.json()["error"]["code"] == "unauthorized", no_header.text

            bad_token = self.client.post(
                "/v1/reviews",
                json={"diff": _mock_diff("auth")},
                headers=self._auth_headers(token="not-the-real-token"),
            )
            assert bad_token.status_code == 401, f"bad-token request got {bad_token.status_code}, want 401"
            assert bad_token.json()["error"]["code"] == "unauthorized", bad_token.text

            self.results.ok(name)
        except Exception as exc:
            self.results.bad(name, str(exc))

    def step_mock_review_e2e(self) -> str | None:
        name = "mock_review_e2e"
        try:
            nonce = uuid.uuid4().hex[:8]
            diff = _mock_diff(nonce)
            resp = self._post_review(diff, options={"provider": "mock"})
            assert resp.status_code == 202, f"submit got {resp.status_code}: {resp.text}"
            job_id = resp.json()["jobId"]
            assert resp.json()["status"] == "queued", resp.text

            result = self._poll_until_terminal(job_id)
            assert result["status"] == "done", f"job ended as {result['status']}: {result.get('error')}"

            rule_ids = [f["ruleId"] for f in result["findings"]]
            assert rule_ids == ["MOCK-001", "MOCK-007", "MOCK-008"], f"unexpected findings: {result['findings']}"
            assert result["usage"]["cacheHit"] is False, result["usage"]

            self.results.ok(name)
            return job_id
        except Exception as exc:
            self.results.bad(name, str(exc))
            return None

    def step_sse_streaming(self) -> None:
        name = "sse_streaming"
        try:
            nonce = uuid.uuid4().hex[:8]
            diff = _mock_diff(nonce)
            submit = self._post_review(diff, options={"provider": "mock"})
            assert submit.status_code == 202, submit.text
            job_id = submit.json()["jobId"]

            events: list[tuple[str, dict]] = []
            with self.client.stream(
                "GET", f"/v1/reviews/{job_id}/stream", headers=self._auth_headers()
            ) as stream_resp:
                assert stream_resp.status_code == 200, f"stream got {stream_resp.status_code}"
                content_type = stream_resp.headers.get("content-type", "")
                assert content_type.startswith("text/event-stream"), f"unexpected content-type: {content_type}"

                event_type = None
                for line in stream_resp.iter_lines():
                    if line == "":
                        continue
                    if line.startswith("event: "):
                        event_type = line[len("event: "):]
                    elif line.startswith("data: "):
                        assert event_type is not None, "data line with no preceding event line"
                        events.append((event_type, json.loads(line[len("data: "):])))
                        if event_type == "done":
                            break
                        event_type = None

            assert events, "no SSE events received"
            assert events[0] == ("status", {"status": "running"}), f"unexpected first event: {events[0]}"
            assert events[-1][0] == "done", f"unexpected last event: {events[-1]}"

            finding_events = events[1:-1]
            assert all(t == "finding" for t, _ in finding_events), f"non-finding event mid-stream: {finding_events}"
            rule_ids = [d["ruleId"] for _, d in finding_events]
            assert rule_ids == ["MOCK-001", "MOCK-007", "MOCK-008"], f"unexpected streamed findings: {rule_ids}"
            assert events[-1][1]["total"] == len(finding_events), events[-1]

            self.results.ok(name)
        except Exception as exc:
            self.results.bad(name, str(exc))

    def step_idempotency_resubmission(self) -> None:
        name = "idempotency_resubmission"
        try:
            nonce = uuid.uuid4().hex[:8]
            diff = _mock_diff(nonce)
            key = f"smoke-{uuid.uuid4()}"

            first = self._post_review(diff, idempotency_key=key)
            assert first.status_code == 202, first.text
            second = self._post_review(diff, idempotency_key=key)
            assert second.status_code == 202, second.text

            first_job_id = first.json()["jobId"]
            second_job_id = second.json()["jobId"]
            assert first_job_id == second_job_id, (
                f"same Idempotency-Key + same body returned different jobIds: "
                f"{first_job_id} != {second_job_id}"
            )

            different_diff = _mock_diff(uuid.uuid4().hex[:8])
            conflict = self._post_review(different_diff, idempotency_key=key)
            assert conflict.status_code == 409, f"expected 409 on body mismatch, got {conflict.status_code}"
            assert conflict.json()["error"]["code"] == "idempotency_conflict", conflict.text

            self.results.ok(name)
        except Exception as exc:
            self.results.bad(name, str(exc))

    def step_cache_hit_resubmission(self) -> None:
        name = "cache_hit_resubmission"
        try:
            nonce = uuid.uuid4().hex[:8]
            diff = _mock_diff(nonce)

            first = self._post_review(diff)
            assert first.status_code == 202, first.text
            first_job_id = first.json()["jobId"]
            first_result = self._poll_until_terminal(first_job_id)
            assert first_result["status"] == "done", first_result

            second = self._post_review(diff)
            assert second.status_code == 202, second.text
            second_job_id = second.json()["jobId"]
            assert second_job_id != first_job_id, "expected a new jobId even on a cache hit"

            second_result = self._poll_until_terminal(second_job_id)
            assert second_result["status"] == "done", second_result
            assert second_result["usage"]["cacheHit"] is True, second_result["usage"]
            assert second_result["findings"] == first_result["findings"], (
                second_result["findings"], first_result["findings"],
            )

            self.results.ok(name)
        except Exception as exc:
            self.results.bad(name, str(exc))

    def step_rate_limit_boundary(self) -> None:
        name = "rate_limit_boundary"
        try:
            limit = self.rate_limit_per_minute
            assert limit is not None, "rate limit unknown -- did the /spec step run and pass?"

            # `post_count` already reflects every POST /v1/reviews issued by
            # earlier steps in *this* run, since they share the same sliding
            # window on the server. Top up to exactly `limit` requests total,
            # then the next one must be rejected.
            remaining = limit - self.post_count
            assert remaining >= 0, (
                f"already issued {self.post_count} POSTs before this step, "
                f"more than the declared limit of {limit} -- cannot cleanly test the boundary"
            )

            for i in range(remaining):
                resp = self._post_review(_mock_diff(f"rl{i}-{uuid.uuid4().hex[:6]}"))
                assert resp.status_code == 202, (
                    f"request {i + 1}/{remaining} under the limit got {resp.status_code}: {resp.text}"
                )

            over_limit = self._post_review(_mock_diff(f"rl-over-{uuid.uuid4().hex[:6]}"))
            assert over_limit.status_code == 429, (
                f"request beyond the {limit}/min limit got {over_limit.status_code}, want 429"
            )
            assert "retry-after" in {k.lower() for k in over_limit.headers.keys()}, (
                f"429 response missing Retry-After header: {dict(over_limit.headers)}"
            )
            assert over_limit.json()["error"]["code"] == "rate_limited", over_limit.text

            self.results.ok(name)
        except Exception as exc:
            self.results.bad(name, str(exc))

    def run(self) -> int:
        self.step_health()
        self.step_spec()
        self.step_auth_rejection()
        self.step_mock_review_e2e()
        self.step_sse_streaming()
        self.step_idempotency_resubmission()
        self.step_cache_hit_resubmission()
        # Run last: it deliberately exhausts the POST rate limit for the
        # remainder of the current sliding window on the target server.
        self.step_rate_limit_boundary()

        total = len(self.results.passed) + len(self.results.failed)
        print()
        print(f"{len(self.results.passed)}/{total} steps passed")
        if self.results.failed:
            print(f"FAILED: {', '.join(self.results.failed)}")
            return 1
        print("ALL SMOKE TESTS PASSED")
        return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "base_url", nargs="?", default=os.environ.get("SMOKE_BASE_URL"),
        help="Base URL of the deployed service, e.g. https://your-app.onrender.com "
             "(or set SMOKE_BASE_URL)",
    )
    parser.add_argument(
        "token", nargs="?", default=os.environ.get("SMOKE_AUTH_TOKEN"),
        help="Bearer token for the deployment's AUTH_TOKEN (or set SMOKE_AUTH_TOKEN)",
    )
    parser.add_argument("--base-url", dest="base_url_flag", default=None, help="Alternative to positional base_url")
    parser.add_argument("--token", dest="token_flag", default=None, help="Alternative to positional token")
    args = parser.parse_args(argv)

    base_url = args.base_url_flag or args.base_url
    token = args.token_flag or args.token
    if not base_url or not token:
        parser.error(
            "base URL and token are required (positional args, --base-url/--token flags, "
            "or SMOKE_BASE_URL/SMOKE_AUTH_TOKEN env vars)"
        )
    args.base_url = base_url
    args.token = token
    return args


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    print(f"Running smoke test against {args.base_url}\n")

    smoke = SmokeTest(args.base_url, args.token)
    try:
        return smoke.run()
    finally:
        smoke.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
