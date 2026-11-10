"""SQLite storage. One short-lived connection per operation (safe across threads/processes)."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_login_at REAL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    prefix       TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    created_at   REAL NOT NULL,
    created_by   TEXT,
    last_used_at REAL,
    revoked_at   REAL
);
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    session_date     TEXT NOT NULL,
    client_ref       TEXT,
    source           TEXT NOT NULL,
    submitted_by     TEXT,
    synced           INTEGER NOT NULL DEFAULT 1,
    options          TEXT NOT NULL DEFAULT '{}',
    files            TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL,
    stage            TEXT,
    percent          REAL NOT NULL DEFAULT 0,
    message          TEXT,
    error            TEXT,
    results          TEXT NOT NULL DEFAULT '{}',
    log_tail         TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
"""

JSON_FIELDS = ("options", "files", "results")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "oidc_sub" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN oidc_sub TEXT")
        if "can_publish" not in cols:   # existing accounts keep publishing to Nextcloud
            c.execute("ALTER TABLE users ADD COLUMN can_publish INTEGER NOT NULL DEFAULT 1")
        if "can_view_all" not in cols:  # see every job's output, not only your own (admins always can)
            c.execute("ALTER TABLE users ADD COLUMN can_view_all INTEGER NOT NULL DEFAULT 0")
        jcols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "owner" not in jcols:
            # The account a job belongs to: the signed-in user, or the user who created the API key.
            c.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")
            c.execute("UPDATE jobs SET owner = submitted_by WHERE submitted_by NOT LIKE 'key:%'")
            c.execute("""UPDATE jobs SET owner = (SELECT k.created_by FROM api_keys k
                         WHERE 'key:' || k.name = jobs.submitted_by ORDER BY k.created_at LIMIT 1)
                         WHERE owner IS NULL AND submitted_by LIKE 'key:%'""")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for f in JSON_FIELDS:
        if f in d and isinstance(d[f], str):
            try:
                d[f] = json.loads(d[f])
            except json.JSONDecodeError:
                d[f] = {} if f != "files" else []
    return d


def now() -> float:
    return time.time()


# ----------------------------------------------------------------- jobs

def get_job(job_id: str) -> dict | None:
    with connect() as c:
        return row_to_dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def list_jobs(limit: int = 100) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [row_to_dict(r) for r in rows]  # type: ignore[misc]


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    for f in JSON_FIELDS:
        if f in fields and not isinstance(fields[f], str):
            fields[f] = json.dumps(fields[f])
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def insert_job(job: dict) -> None:
    job = dict(job)
    for f in JSON_FIELDS:
        if f in job and not isinstance(job[f], str):
            job[f] = json.dumps(job[f])
    cols = ", ".join(job.keys())
    qs = ", ".join("?" for _ in job)
    with connect() as c:
        c.execute(f"INSERT INTO jobs ({cols}) VALUES ({qs})", tuple(job.values()))


def delete_job(job_id: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
