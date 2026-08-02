# Build Spec: AI Diff Review Service

Give this whole file to Claude Code as your first prompt. Tell it to read it fully before writing any code, and to build in the phases listed at the bottom (don't let it jump straight to a finished app in one shot — review after each phase).

## Stack (locked in)

- **Language/framework**: Python + FastAPI
- **Diff parsing**: `unidiff` (or `whatthepatch`) library for hunk/line-number parsing, plus custom logic for multi-line rules (empty catch blocks) and chunking
- **Storage**: SQLite file on a persistent disk (Render Starter tier with a disk mounted)
- **Concurrency**: in-process async worker pool (asyncio + semaphore, max 4 concurrent), simple in-memory or SQLite-backed queue for anything beyond that
- **LLM provider**: Groq API (OpenAI-compatible client), model e.g. `llama-3.3-70b-versatile`
- **Deployment**: Render, Starter plan, persistent disk attached, GitHub-connected auto-deploy

## Environment variables

```
AUTH_TOKEN=<bearer token you generated locally>
GROQ_API_KEY=<your groq key>
GROQ_MODEL=llama-3.3-70b-versatile
DATABASE_PATH=/data/reviews.db     # /data = the persistent disk mount point on Render
PORT=8000
```

## Full API contract (copy exactly — do not deviate)

### GET /health (public, no auth)
`200` → `{ "status": "ok", "version": "<semver>", "uptimeSeconds": <number> }`

### GET /spec (public, no auth)
`200` → must match real declared behavior exactly:
```json
{
  "specVersion": "1.0",
  "providers": ["mock", "llm"],
  "limits": {
    "maxPayloadBytes": 1048576,
    "chunkBytes": 65536,
    "maxConcurrentJobs": 4,
    "rateLimitPerMinute": 30
  }
}
```

### Auth
All `/v1/*` routes (every method incl. GET) require `Authorization: Bearer <AUTH_TOKEN>`.
Missing/wrong → `401` with error envelope.

### POST /v1/reviews
Body:
```json
{
  "diff": "<unified diff, required>",
  "options": { "provider": "mock" | "llm", "maxFindings": 100 }
}
```
- `202` → `{ "jobId": "...", "status": "queued" }`, processing is async — never block this response on the actual scan.
- Payload > 1 MiB → `413`. Invalid JSON → `400`. `diff` missing/empty/unparseable → `422`. Unknown fields ignored (not an error).
- `Idempotency-Key` header: same key + byte-identical body → same `jobId`. Same key + different body → `409`.
- Caching: byte-identical `{diff, options}` submitted again (any key or none) → `cacheHit: true`, findings identical to first run, no rework.

### GET /v1/reviews/{jobId}
`200` → `{ jobId, status, findings (when done), usage: { inputBytes, chunks, cacheHit } }`.
Unknown jobId → `404`. Diffs ≤64 KiB must reach `done` within 30s.

### GET /v1/reviews/{jobId}/stream
SSE, `Content-Type: text/event-stream`.
- `status` events on transitions, `finding` events one per finding as discovered, final `done` event `{"total": <count>, "usage": {...}}`, then close.
- **Critical**: connecting to an already-finished job must replay the identical event sequence. This means every emitted event must be persisted (e.g. an `events` table keyed by jobId + sequence number), and the stream endpoint just reads from that table live or historically — same code path either way.

### Error envelope (all non-2xx)
```json
{ "error": { "code": "<machine_code>", "message": "<human text>" } }
```
Codes: `unauthorized`, `payload_too_large`, `invalid_json`, `invalid_diff`, `idempotency_conflict`, `not_found`, `rate_limited`, `internal`

## Finding object
```json
{
  "id": "MOCK-003:src/db.ts:41",
  "ruleId": "MOCK-003",
  "path": "src/db.ts",
  "line": 41,
  "severity": "critical" | "high" | "medium" | "low",
  "category": "security" | "correctness" | "performance" | "style",
  "title": "<short>",
  "evidence": "<the offending added line, verbatim>"
}
```
`id` = `ruleId:path:line`. Order everywhere (GET response + SSE stream): `path` asc → `line` asc → `ruleId` asc. Dedupe by `id`.

## Mock provider rules — implement EXACTLY, applies only to added (`+`) lines, excluding the `+++` header. `line` = line number in the NEW file (compute from `@@ -a,b +c,d @@` hunk headers).

| ruleId | severity | category | trigger | title |
|---|---|---|---|---|
| MOCK-001 | critical | security | contains `eval(` | eval usage |
| MOCK-002 | critical | security | regex `/(api[_-]?key|secret|token)\s*[:=]\s*['"][A-Za-z0-9_\-]{16,}['"]/i` | hardcoded credential |
| MOCK-003 | high | security | SQL keyword (SELECT/INSERT/UPDATE/DELETE) inside a string concatenated with `+` | SQL string concatenation |
| MOCK-004 | high | correctness | empty catch block (may span lines; report the `catch` line) | swallowed exception |
| MOCK-005 | medium | correctness | `== null` or `!= null` | loose null comparison |
| MOCK-006 | medium | performance | `JSON.parse(JSON.stringify(` | deep-clone via JSON |
| MOCK-007 | low | style | contains `console.log(` | console.log left in |
| MOCK-008 | low | style | contains `TODO` or `FIXME` | unresolved marker |
| MOCK-INJ | critical | security | case-insensitive: "ignore previous instructions" / "disregard all prior" / "you are now" | prompt-injection content |

**MOCK-INJ is a trap — write a test that proves it.** The finding must be reported, but the content must never change control flow anywhere in your service (no special-casing, no early return, no altered validation). Treat it as inert text, full stop.

`maxFindings` truncates the *returned* list only. `usage` always reflects the full scan.

## Chunking
Diffs > 64 KiB → split into ≤64 KiB chunks, **only at file boundaries** (a file's diff never spans two chunks; one oversized file = its own chunk, even if >64 KiB). `usage.chunks` = chunk count. Output must be byte-identical to an unchunked scan — same findings, same order, no dupes/losses.

## Rate limiting
Applies to `POST /v1/reviews` only (GETs never limited). Must sustain 30/min. Beyond declared burst → `429` + `Retry-After` header + error envelope. Never 5xx under load.

## Concurrency
≥4 jobs processing simultaneously. A queued 5th must not fail, just wait.

## LLM provider
Same pipeline (parsing → chunking → rule/model step → same finding shape → same ordering/caching/streaming). The "rule" step calls Groq instead of pattern matching. If Groq is unreachable/errors/times out → job ends as `status: "failed"` with a clear `error` message in the job record — **never crash the process**. Test this by temporarily using a broken API key and confirming graceful failure.

## Build phases (build and verify in this order — don't skip ahead)

1. **Skeleton**: FastAPI app boots, `/health` + `/spec` work, auth middleware on all `/v1/*`, SQLite schema created on startup (jobs table, events table, cache table).
2. **Diff parser**: parse raw diff text → list of `{path, line, content}` for added lines. Write unit tests with 3-4 hand-crafted diffs before touching anything else.
3. **Mock rule engine**: implement all 9 rules as pure functions over parsed lines. One test diff per rule (positive + negative case), plus one dedicated MOCK-INJ inertness test.
4. **Job system**: `POST /v1/reviews` creates a job row (status=queued), returns 202 immediately. Background worker pool (asyncio semaphore, max 4 concurrent) picks up queued jobs, runs parser + rules, writes findings, updates status. `GET /v1/reviews/{id}` reads job state.
5. **Chunking**: add 64 KiB file-boundary splitting to the parser step. Test: one big diff run chunked vs a modified <64KiB version run unchunked — findings must match exactly (adjust test diff size deliberately to compare).
6. **SSE + event persistence**: every state transition and finding gets written to an `events` table with a sequence number. `/stream` endpoint reads from this table (tail -f style if live, full replay if job already done) — same underlying data source for both cases, no separate "live" logic.
7. **Idempotency + caching**: idempotency table keyed by `Idempotency-Key` → jobId + body hash (409 on mismatch). Separate cache table keyed by hash of `{diff, options}` → jobId (works with no key too). Test both independently.
8. **Rate limiting**: token bucket or sliding window on POST only, 30/min, `429` + `Retry-After` beyond burst. Add this after your own manual testing is mostly done so it doesn't get in your way earlier.
9. **LLM provider**: Groq call behind the same pipeline. Test both success path and induced-failure path (bad key) before moving on.
10. **Deploy to Render** (Starter plan + persistent disk mounted at `/data`, env vars set). Re-run your full manual test checklist **against the live URL**, not localhost.
11. **SUBMISSION.md** — write last, once you know exactly what you tested and skipped.

At the end of each phase, stop and manually verify against the contract before moving to the next phase — don't let all of this get built in one shot with testing deferred to the end.
