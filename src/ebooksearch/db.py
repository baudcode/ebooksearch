"""SQLite + FTS5 schema and connection helpers.

One writer connection lives in the IndexManager's writer thread; readers go
through short-lived connections obtained via :func:`connect`. WAL mode lets
readers run concurrently with the single writer.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id              INTEGER PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    rel_path        TEXT NOT NULL,
    filename        TEXT NOT NULL,
    ext             TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    content_hash    TEXT NOT NULL,
    title           TEXT,
    author          TEXT,
    language        TEXT,
    publisher       TEXT,
    pub_date        TEXT,
    page_count      INTEGER,
    indexed_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_books_indexed_at ON books(indexed_at DESC);
CREATE INDEX IF NOT EXISTS idx_books_path ON books(path);
-- Lookup index for move detection: same content_hash, different path.
CREATE INDEX IF NOT EXISTS idx_books_content_hash ON books(content_hash);

-- External-content FTS5: FTS holds no duplicate text; triggers keep it in sync.
-- Extension point: add a `content` column here (and to triggers + extractor
-- pipeline) to make full-text-of-contents searchable.
CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    title,
    author,
    filename,
    content='books',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS books_ai AFTER INSERT ON books BEGIN
    INSERT INTO books_fts(rowid, title, author, filename)
    VALUES (new.id, new.title, new.author, new.filename);
END;

CREATE TRIGGER IF NOT EXISTS books_ad AFTER DELETE ON books BEGIN
    INSERT INTO books_fts(books_fts, rowid, title, author, filename)
    VALUES ('delete', old.id, old.title, old.author, old.filename);
END;

CREATE TRIGGER IF NOT EXISTS books_au AFTER UPDATE ON books BEGIN
    INSERT INTO books_fts(books_fts, rowid, title, author, filename)
    VALUES ('delete', old.id, old.title, old.author, old.filename);
    INSERT INTO books_fts(rowid, title, author, filename)
    VALUES (new.id, new.title, new.author, new.filename);
END;

CREATE TABLE IF NOT EXISTS index_runs (
    id                  INTEGER PRIMARY KEY,
    trigger             TEXT NOT NULL,
    status              TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    added               INTEGER NOT NULL DEFAULT 0,
    updated             INTEGER NOT NULL DEFAULT 0,
    removed             INTEGER NOT NULL DEFAULT 0,
    skipped             INTEGER NOT NULL DEFAULT 0,
    error_count         INTEGER NOT NULL DEFAULT 0,
    duration_seconds    REAL
);

CREATE INDEX IF NOT EXISTS idx_index_runs_started_at ON index_runs(started_at DESC);

-- Full per-run error log. The live `ProgressState.errors` list caps at
-- MAX_ERRORS for SSE-payload sanity; this table keeps the rest so the UI
-- can lazy-fetch the full list on demand.
CREATE TABLE IF NOT EXISTS index_run_errors (
    id      INTEGER PRIMARY KEY,
    run_id  INTEGER NOT NULL,
    path    TEXT NOT NULL,
    message TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES index_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_index_run_errors_run_id ON index_run_errors(run_id);
"""


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + sane defaults.

    ``check_same_thread=False`` so the writer thread (and short-lived reader
    connections from FastAPI's threadpool) can both use it. Callers are
    responsible for not sharing a single connection across threads.
    """
    if read_only:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
    else:
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: Path) -> None:
    """Create tables, FTS table, and triggers if they don't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
