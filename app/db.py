import sqlite3
from pathlib import Path

from app.config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    provider TEXT NOT NULL,
    diff TEXT NOT NULL,
    options TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    error TEXT,
    usage_input_bytes INTEGER,
    usage_chunks INTEGER,
    usage_cache_hit INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    rule_id TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    evidence TEXT NOT NULL,
    PRIMARY KEY (job_id, id)
);

CREATE TABLE IF NOT EXISTS events (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    body_hash TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cache (
    body_hash TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    created_at TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
