# ai-diff-reviewer

Async HTTP service that reviews unified diffs for security/correctness/style issues, with pluggable mock and LLM (Groq) providers, SSE streaming, caching, and idempotency.

## Tech stack

- Python 3.11 + FastAPI
- SQLite (file-based, persistent disk in deployment)
- `unidiff` for diff/hunk parsing
- Groq API (OpenAI-compatible client) for the LLM provider
- pytest for testing

## Quick start

```bash
git clone <repo-url>
cd ai-diff-reviewer

python -m venv venv
.\venv\Scripts\Activate.ps1   # Windows PowerShell

pip install -r requirements.txt

copy .env.example .env
# then edit .env and fill in AUTH_TOKEN / GROQ_API_KEY

uvicorn app.main:app --reload

pytest
```

## Environment variables

| Variable | Purpose | Required |
|---|---|---|
| `AUTH_TOKEN` | Bearer token required on all `/v1/*` routes | yes |
| `GROQ_API_KEY` | API key for the Groq LLM provider | yes |
| `GROQ_MODEL` | Groq model name (e.g. `llama-3.3-70b-versatile`) | yes |
| `DATABASE_PATH` | Path to the SQLite file | yes |
| `PORT` | Port the server listens on | no, defaults to 8000 |

See [.env.example](.env.example) for a template.

## API overview

| Route | Description |
|---|---|
| `GET /health` | Liveness check, no auth |
| `GET /spec` | Declared service limits and supported providers, no auth |
| `POST /v1/reviews` | Submit a diff for review, returns a job id immediately |
| `GET /v1/reviews/{id}` | Poll job status and findings |
| `GET /v1/reviews/{id}/stream` | SSE stream of status/finding events, live or replayed |

All `/v1/*` routes require `Authorization: Bearer <AUTH_TOKEN>`. Full request/response contract, error codes, and finding schema are in [BUILD_SPEC.md](BUILD_SPEC.md).

## Running tests

```bash
pytest
```

67 tests passing, covering the diff parser, rule engine, job pipeline, chunking, SSE streaming, rate limiting, concurrency, and the LLM provider.

## Architecture

`POST /v1/reviews` validates and queues a job and returns `202` without waiting on the scan; a semaphore-bounded async worker pool (max 4 concurrent) picks up jobs, runs the diff parser, hands parsed lines to the selected provider (mock rules or Groq), and logs every status change and finding to an `events` table. Both the polling endpoint and the SSE stream read from that same table, so live and replayed streams can't drift apart. Full diagrams in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Deployment

Deployed on Render (Starter plan) with a persistent disk mounted for the SQLite database file.

## More detail

See [SUBMISSION.md](SUBMISSION.md) for the full write-up: provider design, how the cross-cutting behavior (chunking, idempotency/caching, SSE replay, concurrency) was verified, production issues hit during deploy, and what was skipped.
