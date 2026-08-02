# SUBMISSION.md

## Architecture

FastAPI on top of SQLite, sitting on a persistent disk. No external queue,
no Redis, no separate cache service. `POST /v1/reviews` validates the
request, checks idempotency and cache, writes a `queued` job row, and
returns `202`. Nothing in that handler ever waits on the actual scan.

A pool of async workers, capped at 4 through a semaphore, picks jobs up:
runs the diff parser, hands the parsed lines to whichever provider was
requested, writes findings, and logs every status change and finding into
an `events` table with a sequence number per job. That table is the only
thing `GET /v1/reviews/{id}/stream` reads from, whether the job finished
five seconds ago or an hour ago. Live streaming and replay are the same
code path. I did that on purpose. Two separate implementations of "send
events to a client" is exactly the kind of thing that quietly drifts apart
later, and I didn't want to find that out the hard way.

## Provider design

Both providers sit behind the same interface. Take parsed `{path, line,
content}` tuples, hand back findings in the same shape. `mock`
(`app/rules.py`) runs the 9 rules as pattern checks, plus a run-based scan
for MOCK-004's multi-line empty-catch detection. `llm` (`app/llm_provider.py`)
sends the same parsed lines to Groq's hosted Llama 3.3 70B, with a system
prompt that tells the model, explicitly, to treat anything in the diff,
including text that looks like an instruction, as content to review and
nothing more. Past that point, sorting, dedup, event emission, and caching
don't know or care which provider ran.

Two rule interpretations worth flagging rather than burying. MOCK-003 (SQL
string concatenation) checks for a quoted SQL keyword on a line that also
contains a `+`. That's a co-occurrence heuristic, not a real parse. It
can't confirm the `+` is actually next to that string. MOCK-004 has a
similar honest limitation: it can only catch an empty catch block if the
closing brace shows up in the same run of added lines the diff exposes.
Add a `catch` line without touching its closing brace, and the rule has no
way to know. It stays quiet instead of guessing, which felt like the
right failure mode.

## Verifying the cross-cutting stuff

Most of the actual effort went here. A happy-path pass on any of this
doesn't prove much.

**Chunking.** I tested a file whose own raw diff text is over 64 KiB,
flanked by two small files, to confirm the big one gets isolated in its
own chunk on both sides. Separately, I planted three findings (`eval`,
`console.log`, `TODO`) across what would be a chunk boundary and checked
that a full scan still returns exactly those three, correctly ordered, no
duplicates. Chunk counting follows the 64 KiB file-boundary rule. The
actual scan always runs on the whole diff in one pass. Splitting the scan
itself is exactly what would introduce the duplicate or missing-finding
risk chunking exists to prevent, so I didn't.

**Idempotency vs. caching.** These are easy to conflate, so I tested them
apart on purpose. Same idempotency key, same body, submitted twice:
identical `jobId` both times. Same body with no key (or a different key),
submitted twice: a fresh `jobId` the second time, `cacheHit: true`, and a
findings list checked against the first run's. Not same length. Equal.

**SSE replay.** I connected to `/stream` right after submitting, then
again after a separate job had already finished with nobody watching, and
diffed the two event sequences. They matched. Along the way I found a
real bug. A job that ends in `status: failed` never emits a `done` event,
so a client streaming that job just hangs. Forever, in practice. Fixed it
by treating a terminal `status: failed` event as equally valid for
closing the stream.

**Concurrency.** First pass at this test submitted 6 jobs and asserted no
more than 4 were ever seen `running`. It passed. The actual sampled max
was 0, though, because the mock scan finishes in single-digit milliseconds
and the polling loop never caught one mid-flight. More on this below,
since fixing it properly took two tries.

I ran all of this twice: once locally, once again against the deployed
Render instance, including a forced restart to confirm the SQLite disk
actually survives it. It does. Same job, same findings, after a full
process restart. I later re-ran the live pass a second time after a round
of hardening fixes, adding a few checks that were missing the first time:
the exact payload boundary (1,048,576 bytes accepted, 1,048,577 rejected),
chunking on a genuinely large diff against the live instance (3 chunks,
correct output, no duplicates), and a real prompt-injection attempt
against the live Groq path rather than just the deterministic mock rule.
That last one mattered to see with my own eyes: the model flagged the
injected comment as its own finding instead of obeying it, and still
caught the real `eval()` vulnerability sitting two lines below it.

Final test count: 67 passing, across the diff parser, rule engine, job
pipeline, chunking, streaming, rate limiting, concurrency, and the LLM
provider.

## Two deploy problems that actually bit me

Worth writing down because they happened, and because "it runs on my
machine" proves less than it feels like it does.

Render defaulted the build to Python 3.14, which doesn't have a prebuilt
wheel yet for `pydantic-core==2.27.2`. Pip fell back to compiling it from
source through maturin and cargo, and that failed because Render's build
sandbox has a read-only cargo cache directory at exactly the path it
needed to write to. Nothing wrong with my code, just a mismatch between a
pinned dependency and a runtime default newer than the wheel existed for.
Fixed it with a `.python-version` file pinning 3.11.9, where the wheel
already exists and nothing has to compile.

Separately, my first `render.yaml` used `runtime: python`. That's not a
field Render's blueprint schema recognizes; it wants `env: python`. The
error on the dashboard was close to useless ("a Blueprint file was found,
but there was an issue," no further detail anywhere on the page), so I
ended up comparing my file against Render's own docs line by line to
catch it.

Neither of these showed up in local testing. That's exactly why I didn't
treat "runs on my machine" as good enough on its own.

## An AI suggestion I rejected

While building the LLM provider's failure handling, Claude Code noticed
on its own that a job ending in `status: failed` never emits a `done`
event. A client streaming `/stream` for that job just hangs, since the
stream generator only closed on `done`. Its own note at the time: "This
same gap already exists today for mock-provider failures... I left it
alone as out of scope, but flagging in case you want it addressed
separately."

I didn't take that. SSE correctness, replay included, is explicitly in
the scored checklist, and a stream that never terminates on a real
failure path is precisely the kind of thing their probes exist to catch.
I asked for the fix right away, then wrote two tests for it. One confirms
a live stream on a failing job terminates correctly. One confirms replay
after the fact does too. Both pass.

The concurrency test above is the second example, and honestly the more
interesting one. The flaw wasn't in the code at all. The semaphore worked
fine the whole time. It was in the test: `0 <= 4` is a true statement, it
just didn't verify anything. I asked for a test-only delay hook, off by
default, set to about 0.3 seconds only inside that one test, to slow the
mock scans down enough for the poller to actually catch jobs mid-flight.
The re-run showed a clean run of exactly 4 concurrent jobs, dropping to 2
as the remainder finished up.

That fix didn't fully hold up on its own, either. Sampling concurrency
through HTTP polling turned out to be flaky in a different way. Network
jitter and scheduling gaps make it an unreliable way to observe an
internal invariant, no matter how long the delay is. I moved the check
in-process instead: a simple counter around the exact window the
semaphore holds, verified clean over 20 consecutive runs. The original
HTTP-based check stayed too, as a separate test for a separate question.
One test asks "is concurrency bounded correctly," the other asks "did the
jobs actually finish right." They don't have to be the same test.

## What I'd do with more time

- The rate limiter's sliding window lives in memory and resets on a
  restart. Fine for 48 hours, not fine for anything real.
- MOCK-003 deserves an actual tokenizer pass instead of the co-occurrence
  heuristic, so it stops flagging lines where the `+` has nothing to do
  with the SQL string.
- Basic structured logging and a couple of metrics, job latency and cache
  hit rate mainly. Pointless for a 48-hour throwaway service, necessary
  for a real one.
- SQLite-on-disk is fine for one instance. Anything that needed to scale
  past a single box would want Postgres instead.
- Already built rather than just planned: `scripts/smoke_test.py` hits
  the live URL end to end and exits non-zero on any failure, so I'm not
  re-running the same curl commands by hand every time I touch something.