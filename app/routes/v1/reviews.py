import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.auth import error_response
from app.config import MAX_PAYLOAD_BYTES
from app.db import get_connection
from app.diff_parser import DiffParseError, parse_diff, split_into_chunks
from app.rules import run_mock_rules

router = APIRouter()

DEFAULT_MAX_FINDINGS = 100
STREAM_POLL_INTERVAL_SECONDS = 0.3
_SEMAPHORE = asyncio.Semaphore(4)
_background_tasks: set[asyncio.Task] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_options(raw_options) -> dict:
    if not isinstance(raw_options, dict):
        raw_options = {}

    provider = raw_options.get("provider")
    if provider not in ("mock", "llm"):
        provider = "mock"

    max_findings = raw_options.get("maxFindings", DEFAULT_MAX_FINDINGS)
    if not isinstance(max_findings, int) or isinstance(max_findings, bool) or max_findings < 0:
        max_findings = DEFAULT_MAX_FINDINGS

    return {"provider": provider, "maxFindings": max_findings}


def _diff_hash(diff: str, options: dict) -> str:
    canonical = json.dumps({"diff": diff, "options": options}, sort_keys=True)
    return _hash_bytes(canonical.encode("utf-8"))


def _sorted_findings(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: (f["path"], f["line"], f["ruleId"]))


def _row_to_finding(row) -> dict:
    return {
        "id": row["id"],
        "ruleId": row["rule_id"],
        "path": row["path"],
        "line": row["line"],
        "severity": row["severity"],
        "category": row["category"],
        "title": row["title"],
        "evidence": row["evidence"],
    }


def _emit_event(conn, job_id: str, event_type: str, data: dict) -> None:
    next_seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE job_id = ?",
        (job_id,),
    ).fetchone()["next_seq"]
    conn.execute(
        "INSERT INTO events (job_id, seq, event_type, data, created_at) VALUES (?, ?, ?, ?, ?)",
        (job_id, next_seq, event_type, json.dumps(data), _now()),
    )


def _insert_job(conn, job_id: str, status: str, provider: str, diff: str, options: dict, body_hash: str) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO jobs (id, status, provider, diff, options, body_hash,
                           usage_input_bytes, usage_chunks, usage_cache_hit,
                           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)
        """,
        (job_id, status, provider, diff, json.dumps(options), body_hash, now, now),
    )


def _update_job_status(conn, job_id: str, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, error, _now(), job_id),
    )


def _finalize_job_usage(conn, job_id: str, input_bytes: int, chunks: int, cache_hit: bool) -> None:
    conn.execute(
        "UPDATE jobs SET usage_input_bytes = ?, usage_chunks = ?, usage_cache_hit = ?, updated_at = ? WHERE id = ?",
        (input_bytes, chunks, int(cache_hit), _now(), job_id),
    )


def _insert_finding(conn, job_id: str, finding: dict) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO findings (id, job_id, rule_id, path, line, severity, category, title, evidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            finding["id"], job_id, finding["ruleId"], finding["path"], finding["line"],
            finding["severity"], finding["category"], finding["title"], finding["evidence"],
        ),
    )


def _record_idempotency_key(conn, key: str, body_hash: str, job_id: str) -> None:
    conn.execute(
        "INSERT INTO idempotency_keys (key, body_hash, job_id, created_at) VALUES (?, ?, ?, ?)",
        (key, body_hash, job_id, _now()),
    )


def _process_job_sync(job_id: str) -> None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return

        diff = row["diff"]
        options = json.loads(row["options"])

        _update_job_status(conn, job_id, "running")
        _emit_event(conn, job_id, "status", {"status": "running"})
        conn.commit()

        try:
            parsed_lines = parse_diff(diff)
            findings = _sorted_findings(run_mock_rules(parsed_lines))

            for finding in findings:
                _insert_finding(conn, job_id, finding)
                _emit_event(conn, job_id, "finding", finding)

            usage = {
                "inputBytes": len(diff.encode("utf-8")),
                "chunks": len(split_into_chunks(diff)),
                "cacheHit": False,
            }
            _finalize_job_usage(conn, job_id, usage["inputBytes"], usage["chunks"], usage["cacheHit"])
            _update_job_status(conn, job_id, "done")
            _emit_event(conn, job_id, "done", {"total": len(findings), "usage": usage})
            conn.commit()

            diff_hash = _diff_hash(diff, options)
            existing_cache = conn.execute(
                "SELECT job_id FROM cache WHERE body_hash = ?", (diff_hash,)
            ).fetchone()
            if existing_cache is None:
                conn.execute(
                    "INSERT INTO cache (body_hash, job_id, created_at) VALUES (?, ?, ?)",
                    (diff_hash, job_id, _now()),
                )
                conn.commit()
        except Exception as exc:
            conn.rollback()
            _update_job_status(conn, job_id, "failed", error=str(exc))
            conn.commit()
    finally:
        conn.close()


def _replay_cached_job_sync(new_job_id: str, cached_job_id: str) -> None:
    conn = get_connection()
    try:
        cached_findings_rows = conn.execute(
            "SELECT * FROM findings WHERE job_id = ? ORDER BY path ASC, line ASC, rule_id ASC",
            (cached_job_id,),
        ).fetchall()
        findings = [_row_to_finding(r) for r in cached_findings_rows]

        cached_job = conn.execute("SELECT * FROM jobs WHERE id = ?", (cached_job_id,)).fetchone()
        input_bytes = cached_job["usage_input_bytes"] if cached_job else 0

        _update_job_status(conn, new_job_id, "running")
        _emit_event(conn, new_job_id, "status", {"status": "running"})
        conn.commit()

        for finding in findings:
            _insert_finding(conn, new_job_id, finding)
            _emit_event(conn, new_job_id, "finding", finding)

        usage = {"inputBytes": input_bytes, "chunks": 1, "cacheHit": True}
        _finalize_job_usage(conn, new_job_id, usage["inputBytes"], usage["chunks"], True)
        _update_job_status(conn, new_job_id, "done")
        _emit_event(conn, new_job_id, "done", {"total": len(findings), "usage": usage})
        conn.commit()
    finally:
        conn.close()


async def _process_job(job_id: str) -> None:
    async with _SEMAPHORE:
        await asyncio.to_thread(_process_job_sync, job_id)


async def _replay_cached_job(new_job_id: str, cached_job_id: str) -> None:
    await asyncio.to_thread(_replay_cached_job_sync, new_job_id, cached_job_id)


def _schedule(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@router.post("/reviews", status_code=202)
async def create_review(request: Request):
    body = await request.body()

    if len(body) > MAX_PAYLOAD_BYTES:
        raise error_response(413, "payload_too_large", "Request body exceeds maximum payload size")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise error_response(400, "invalid_json", "Request body is not valid JSON")

    diff = payload.get("diff") if isinstance(payload, dict) else None
    if not isinstance(diff, str) or diff.strip() == "":
        raise error_response(422, "invalid_diff", "diff field is required and must be a non-empty string")

    try:
        parse_diff(diff)
    except DiffParseError as exc:
        raise error_response(422, "invalid_diff", f"diff could not be parsed: {exc}")

    options = _normalize_options(payload.get("options") if isinstance(payload, dict) else None)

    idempotency_key = request.headers.get("Idempotency-Key")
    body_hash = _hash_bytes(body)

    conn = get_connection()
    try:
        if idempotency_key:
            existing = conn.execute(
                "SELECT job_id, body_hash FROM idempotency_keys WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["body_hash"] != body_hash:
                    raise error_response(
                        409, "idempotency_conflict",
                        "Idempotency-Key already used with a different request body",
                    )
                job_row = conn.execute(
                    "SELECT status FROM jobs WHERE id = ?", (existing["job_id"],)
                ).fetchone()
                return {"jobId": existing["job_id"], "status": job_row["status"] if job_row else "queued"}

        diff_hash = _diff_hash(diff, options)
        cache_row = conn.execute("SELECT job_id FROM cache WHERE body_hash = ?", (diff_hash,)).fetchone()

        job_id = str(uuid.uuid4())
        _insert_job(conn, job_id, "queued", options["provider"], diff, options, body_hash)
        conn.commit()

        if idempotency_key:
            _record_idempotency_key(conn, idempotency_key, body_hash, job_id)
            conn.commit()

        if cache_row is not None:
            _schedule(_replay_cached_job(job_id, cache_row["job_id"]))
        else:
            _schedule(_process_job(job_id))

        return {"jobId": job_id, "status": "queued"}
    finally:
        conn.close()


@router.get("/reviews/{job_id}")
async def get_review(job_id: str):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise error_response(404, "not_found", "Job not found")

        status = row["status"]
        options = json.loads(row["options"])
        max_findings = options.get("maxFindings", DEFAULT_MAX_FINDINGS)

        response = {"jobId": job_id, "status": status, "findings": [], "usage": None}

        if status == "failed" and row["error"]:
            response["error"] = row["error"]

        if status == "done":
            finding_rows = conn.execute(
                "SELECT * FROM findings WHERE job_id = ? ORDER BY path ASC, line ASC, rule_id ASC",
                (job_id,),
            ).fetchall()
            all_findings = [_row_to_finding(r) for r in finding_rows]
            response["findings"] = all_findings[:max_findings]
            response["usage"] = {
                "inputBytes": row["usage_input_bytes"],
                "chunks": row["usage_chunks"],
                "cacheHit": bool(row["usage_cache_hit"]),
            }

        return response
    finally:
        conn.close()


async def _stream_events(job_id: str):
    last_seq_sent = 0
    while True:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT seq, event_type, data FROM events WHERE job_id = ? AND seq > ? ORDER BY seq",
                (job_id, last_seq_sent),
            ).fetchall()
        finally:
            conn.close()

        done_seen = False
        for row in rows:
            yield f"event: {row['event_type']}\ndata: {row['data']}\n\n"
            last_seq_sent = row["seq"]
            if row["event_type"] == "done":
                done_seen = True

        if done_seen:
            return

        await asyncio.sleep(STREAM_POLL_INTERVAL_SECONDS)


@router.get("/reviews/{job_id}/stream")
async def stream_review(job_id: str):
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()

    if row is None:
        raise error_response(404, "not_found", "Job not found")

    return StreamingResponse(_stream_events(job_id), media_type="text/event-stream")
