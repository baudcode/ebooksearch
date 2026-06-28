"""FTS5 query escaping + ranked search."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from . import db as dbmod

# Tokens: word chars + optional trailing `*` for prefix.
_TOKEN_RE = re.compile(r"[\w]+\*?", re.UNICODE)


def build_match_query(raw: str) -> str:
    """Convert user input into a safe FTS5 MATCH expression.

    - Strips operators (AND/OR/NOT/parens/quotes) by tokenizing word chars.
    - Each token is quoted to neutralize FTS syntax and gets a trailing ``*``
      for prefix matching, so `mob` matches `Moby-Dick` and search-as-you-type
      works without users having to know about FTS syntax.
    - Tokens are joined by AND-style space (FTS5 default behavior).
    """
    tokens = _TOKEN_RE.findall(raw or "")
    pieces: list[str] = []
    for t in tokens:
        core = t.rstrip("*")
        if core:
            pieces.append(f'"{core}"*')
    return " ".join(pieces)


def search_books(db_path: Path, q: str, limit: int, offset: int) -> list[dict]:
    conn = dbmod.connect(db_path, read_only=True)
    try:
        if q.strip():
            match = build_match_query(q)
            if not match:
                return []
            sql = """
                SELECT b.id, b.title, b.author, b.filename, b.ext, b.size_bytes
                FROM books b
                JOIN books_fts f ON f.rowid = b.id
                WHERE books_fts MATCH ?
                ORDER BY bm25(books_fts, 3.0, 2.0, 1.0)
                LIMIT ? OFFSET ?
            """
            rows = conn.execute(sql, (match, limit, offset)).fetchall()
        else:
            sql = """
                SELECT id, title, author, filename, ext, size_bytes
                FROM books
                ORDER BY indexed_at DESC
                LIMIT ? OFFSET ?
            """
            rows = conn.execute(sql, (limit, offset)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def recent_books(db_path: Path, limit: int) -> list[dict]:
    conn = dbmod.connect(db_path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT id, title, author, filename, ext, size_bytes, indexed_at
            FROM books
            ORDER BY indexed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_book(db_path: Path, book_id: int) -> dict | None:
    conn = dbmod.connect(db_path, read_only=True)
    try:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def run_errors(db_path: Path, run_id: int, limit: int, offset: int) -> dict:
    """Paginated list of errors for one indexing run."""
    conn = dbmod.connect(db_path, read_only=True)
    try:
        total = conn.execute(
            "SELECT count(*) AS n FROM index_run_errors WHERE run_id = ?",
            (run_id,),
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT path, message FROM index_run_errors WHERE run_id = ? ORDER BY id LIMIT ? OFFSET ?",
            (run_id, limit, offset),
        ).fetchall()
        return {"run_id": run_id, "total": int(total), "errors": [dict(r) for r in rows]}
    finally:
        conn.close()


def index_runs(db_path: Path, limit: int) -> list[dict]:
    conn = dbmod.connect(db_path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT * FROM index_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    conn = dbmod.connect(db_path, read_only=True)
    try:
        total = conn.execute("SELECT count(*) AS n FROM books").fetchone()["n"]
        last = conn.execute("SELECT * FROM index_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    size = 0
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db_path) + suffix)
        if f.exists():
            size += f.stat().st_size
    return {
        "total_books": int(total),
        "db_size_bytes": size,
        "last_run": dict(last) if last else None,
    }
