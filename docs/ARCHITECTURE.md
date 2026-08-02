# Architecture

This document has four diagrams: the overall pipeline, what actually happens
on a `POST /v1/reviews` call, how `/stream` avoids diverging from the polling
endpoint, and the job state machine. All render directly on GitHub.

## Pipeline overview

```mermaid
flowchart TD
    Client([Client]) --> API["POST /v1/reviews<br/>auth, validation, cache lookup"]
    API --> Queue["Job queued<br/>202 returned immediately"]
    Queue --> Pool["Worker pool<br/>semaphore, max 4 concurrent"]
    Pool --> Parser["Diff parser<br/>unidiff, chunk counting"]
    Pool --> Provider["Provider<br/>mock rules or Groq LLM"]
    Parser --> Provider
    Provider --> Events[("Events table<br/>findings + status log")]
    Events --> Poll["GET /reviews/{id}<br/>poll for status"]
    Events --> Stream["GET /stream<br/>SSE, live or replay"]
```

The parser and provider are drawn as two boxes because they're two separate
modules, but they run inside the same worker call — a job never crosses a
process or queue boundary between "parsed" and "reviewed."

## Request lifecycle: POST /v1/reviews

This is the part that's easy to get wrong, since idempotency and caching are
separate checks that both have to happen before any real work starts.

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API layer
    participant I as idempotency_keys table
    participant Ca as cache table
    participant W as Worker pool

    C->>A: POST /v1/reviews (+ Idempotency-Key?)
    A->>A: size / JSON / diff validity checks
    alt Idempotency-Key present
        A->>I: look up key
        alt key exists, same body hash
            I-->>A: existing jobId
            A-->>C: 202, same jobId
        else key exists, different body hash
            A-->>C: 409 idempotency_conflict
        else key not seen before
            A->>Ca: check diff+options hash
        end
    else no key
        A->>Ca: check diff+options hash
    end
    alt hash found in cache
        Ca-->>A: cached jobId + findings
        A->>A: new jobId, copy findings, cacheHit=true
        A-->>C: 202, new jobId
    else hash not cached
        A->>W: enqueue new job
        A-->>C: 202, new jobId, status=queued
        W->>Ca: store hash -> jobId after success
    end
```

## SSE: why live and replay can't drift apart

`/stream` and `GET /reviews/{id}` both read from the same `events` table.
There's no separate "live" code path — a client connecting after a job has
already finished just gets the same rows in one batch instead of trickling
in over time.

```mermaid
sequenceDiagram
    participant Worker
    participant Events as events table
    participant Stream as GET /stream

    Worker->>Events: write status: running (seq 1)
    Worker->>Events: write finding (seq 2)
    Worker->>Events: write finding (seq 3)
    Worker->>Events: write done (seq 4)

    Note over Stream: Case A: client connects while job is running
    Stream->>Events: poll seq > last_seq_sent
    Events-->>Stream: rows as they're written
    Stream-->>Stream: closes after seq 4 (done)

    Note over Stream: Case B: client connects after job is done
    Stream->>Events: poll seq > 0
    Events-->>Stream: all 4 rows at once
    Stream-->>Stream: closes immediately after seq 4
```

## Job state machine

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> running: worker picks up job
    running --> done: provider returns findings
    running --> failed: provider raises (LLM unreachable, etc.)
    done --> [*]
    failed --> [*]
```

A `failed` job emits a terminal `status: failed` event instead of `done` —
`/stream` treats either one as a valid signal to close the connection, so a
failing job doesn't leave a client waiting forever.