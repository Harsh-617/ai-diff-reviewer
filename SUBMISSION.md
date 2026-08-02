# SUBMISSION.md

## Architecture

FastAPI, SQLite on a persistent disk, no external queue or cache service.
`POST /v1/reviews` validates the request, checks idempotency and cache,
writes a `queued` job row, and returns `202` — nothing in that handler ever
waits on the actual scan. A pool of async workers, capped at 4 through a
semaphore, picks jobs up, runs the diff parser, hands the parsed lines to
whichever provider was requested, writes findings, and logs every status
change and finding to an `events` table with a sequence number per job.
That table is the only thing `GET /v1/reviews/{id}/stream` reads from,
whether the job finished five seconds ago or an hour ago. Live streaming and
replay are the same code path on purpose — I didn't want two implementations
that could quietly drift apart.

## Provider design

Both providers implement one interface: take parsed `{path, line, content}`
tuples and return findings in the same shape. `mock` (`app/rules.py`) runs
the 9 rules as pattern checks, plus a run-based scan for MOCK-004's
multi-line empty-catch detection. `llm` (`app/llm_provider.py`) sends the
same parsed lines to Groq's hosted Llama 3.3 70B, with a system prompt that
explicitly tells the model to treat anything in the diff — including text
that looks like an instruction — as content to review, not something to
act on. Past that point, sorting, dedup, event emission, and caching don't
know or care which provider ran.

Two rule interpretations I want to flag rather than bury: MOCK-003 (SQL
string concatenation) checks for a quoted SQL keyword on a line that also
contains a `+`, which is a co-occurrence heuristic, not a real parse — it
can't confirm the `+` is actually next to that string. And MOCK-004 can only
catch an empty catch block if its closing brace shows up in the same run of
added lines the diff exposes. If a diff adds a `catch` line but the file's
closing brace wasn't touched, the rule has no way to know and correctly
stays quiet rather than guessing.

## Verifying the cross-cutting stuff

This is where most of the actual effort went, since a happy-path pass on
these doesn't mean much.

**Chunking.** I tested a file whose own raw diff text is over 64 KiB,
flanked by two small files, to confirm the big one gets isolated in its own
chunk on both sides. Separately, I planted three findings (`eval`,
`console.log`, `TODO`) across what would be a chunk boundary and checked
that a full scan still returns exactly those three, correctly ordered, no
duplicates. Chunk *counting* follows the 64 KiB file-boundary rule; the
actual scan always runs on the whole diff in one pass, because splitting the
scan itself is what would introduce the duplicate/missing-finding risk
chunking exists to prevent in the first place.

**Idempotency vs. caching.** These are easy to conflate, so I tested them
separately on purpose. Same idempotency key, same body, submitted twice:
identical `jobId` both times. Same body, no key (or a different key),
submitted twice: a fresh `jobId` the second time, `cacheHit: true`, and a
findings list I asserted equal to the first run's — not just same length,
actually equal.

**SSE replay.** I connected to `/stream` right after submitting (live case),
and again after a separate job had already finished with nobody watching
(replay case), and diffed the two event sequences. They matched. Along the
way I found a real bug: a job that ends in `status: failed` never emits a
`done` event, so a client streaming that job would just hang forever. Fixed
by treating a terminal `status: failed` event as equally valid for closing
the stream.

**Concurrency.** First pass at this test submitted 6 jobs and asserted no
more than 4 were ever seen `running`. It passed — but the actual sampled max
was 0, because the mock scan finishes in single-digit milliseconds and the
test's own polling loop never caught one mid-flight. See below; this one's
worth its own explanation.

I ran all of this twice — once locally, once again against the deployed
Render instance, including a forced restart to confirm the SQLite disk
actually survives it (it does; same job, same findings, after a full
process restart).

Final test count: 63 passing, spanning the diff parser, rule engine, job
pipeline, chunking, streaming, rate limiting, concurrency, and the LLM
provider.

## Two production problems I actually hit, not hypothetical ones

Worth including because they're real and because "it built and ran locally"
is not the same claim as "it deploys."

Render defaulted the build to Python 3.14, which doesn't yet have a
prebuilt wheel for `pydantic-core==2.27.2`. Pip fell back to compiling it
from source via maturin/cargo, which then failed because Render's build
sandbox has a read-only cargo cache directory at that path. Nothing wrong
with my code — just a version mismatch between a pinned dependency and a
too-new default runtime. Fixed with a `.python-version` file pinning 3.11.9,
where the wheel exists and nothing needs to compile.

Separately, my first `render.yaml` used `runtime: python`, which isn't a
field Render's blueprint schema recognizes — it wants `env: python`. The
error message on that one was genuinely unhelpful ("a Blueprint file was
found, but there was an issue," no further detail on the page), so I had to
go compare against Render's own docs to catch it.

Neither of these came up in local testing, which is exactly why I didn't
treat "runs on my machine" as sufficient and re-ran the full verification
pass against the live URL afterward.

## An AI suggestion I rejected

While building the LLM provider's failure handling, Claude Code noticed on
its own that a job ending in `status: failed` never emits a `done` event —
so a client streaming `/stream` for that job would hang indefinitely, since
the stream generator only closed on `done`. Its own note at the time:
"This same gap already exists today for mock-provider failures... I left it
alone as out of scope — but flagging in case you want it addressed
separately."

I didn't accept that. SSE correctness, including replay, is explicitly in
the scored checklist, and a stream that never terminates on a real failure
path is precisely the kind of thing their probes would catch. I asked for
the fix immediately, then wrote two tests for it: one confirming a live
stream on a failing job terminates correctly, one confirming replay after
the fact does too. Both pass.

The concurrency test above is the second example, and honestly the more
interesting one, because the flaw wasn't in the code — the semaphore worked
fine the whole time — it was in the *test*. `0 <= 4` was a true statement
that verified nothing. I asked for a test-only delay hook (0 by default,
set to ~0.3s only inside this one test via monkeypatch) to slow the mock
scans down enough for the poller to actually catch jobs mid-flight. The
re-run showed a clean run of exactly 4 concurrent jobs, dropping to 2 as the
remainder (6 % 4) finished up. That's a real proof instead of a coincidence
that happened to pass.

## What I'd do with more time

- The rate limiter's sliding window lives in memory and resets on a
  restart. Fine for a 48-hour window, not fine for anything real.
- MOCK-003 deserves an actual tokenizer pass instead of the co-occurrence
  heuristic, to stop flagging lines where the `+` has nothing to do with
  the SQL string.
- A small smoke-test script that hits the live URL directly, run right
  before submission, instead of me doing that by hand with curl.
- Basic structured logging and a couple of metrics — job latency, cache hit
  rate — useless for a 48-hour throwaway service, necessary for a real one.
- SQLite-on-disk is fine for one instance. Anything that needed to scale
  past a single box would want Postgres instead.