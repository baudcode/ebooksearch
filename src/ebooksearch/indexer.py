"""IndexManager: parallel parse, serial single-writer, live progress.

Architecture
------------
- A ``ThreadPoolExecutor`` parses files in parallel; parse jobs never touch
  the DB (they only read disk and return :class:`ParsedBook`).
- One dedicated writer thread owns the single sqlite3 write connection and
  drains a queue of parse results, upserting in batched transactions.
- Single-flight lock: only one run at a time. A second request while a run
  is active coalesces into one queued follow-up (set of paths union'd).
- WAL means readers (search/download/recent endpoints) run concurrently with
  the writer.

The async-bridge for SSE is owned externally: the manager exposes a sync
``add_listener(callback)`` hook; the FastAPI layer's listener schedules the
event onto its event loop using ``loop.call_soon_threadsafe``.
"""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import db as dbmod
from .extractors import extract, is_ebook_file, iter_ebook_files
from .models import ParsedBook
from .progress import ProgressState

logger = logging.getLogger(__name__)

# Sentinel pushed into the writer queue to signal "drain and exit".
_STOP = object()

# A targeted run whose accumulated path set exceeds this is promoted to a full
# scan — walking the dir with mtime-skip is cheaper than chasing thousands of
# individual paths, and it bounds memory if watchdog floods events during a
# long-running index.
_TARGETED_PROMOTE_THRESHOLD = 1000


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IndexManager:
    def __init__(
        self,
        *,
        db_path: Path,
        ebook_dir: Path,
        workers: int,
        write_batch: int,
        max_file_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self.db_path = db_path
        self.ebook_dir = ebook_dir
        self.workers = max(1, workers)
        self.write_batch = max(1, write_batch)
        self.max_file_bytes = max(0, max_file_bytes)

        self.progress = ProgressState()
        self._listeners: list[Callable[[dict], None]] = []
        self._listeners_lock = threading.Lock()

        # Single-flight + coalescing
        self._run_lock = threading.Lock()  # only one indexing run at a time
        self._queued_followup: Optional[dict] = None  # {"trigger": str, "paths": set|None}
        self._queue_lock = threading.Lock()

        # Long-lived background thread that owns starting runs
        self._dispatcher_wake = threading.Event()
        self._stopping = threading.Event()
        self._dispatcher = threading.Thread(target=self._dispatcher_loop, name="index-dispatcher", daemon=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        dbmod.init_db(self.db_path)
        self._dispatcher.start()

    def stop(self) -> None:
        self._stopping.set()
        self._dispatcher_wake.set()
        if self._dispatcher.is_alive():
            self._dispatcher.join(timeout=10)

    # ------------------------------------------------------------------
    # Public: enqueue runs
    # ------------------------------------------------------------------
    def request_full_scan(self, trigger: str) -> None:
        """Queue a full rescan. Coalesces with any pending request."""
        self._enqueue({"trigger": trigger, "paths": None})

    def request_targeted(self, trigger: str, paths: Iterable[Path]) -> None:
        """Queue targeted updates for a specific set of paths."""
        self._enqueue({"trigger": trigger, "paths": {Path(p) for p in paths}})

    def _enqueue(self, request: dict) -> None:
        paths_info = "full" if request["paths"] is None else f"{len(request['paths'])} paths"
        logger.info("enqueue: trigger=%s %s", request["trigger"], paths_info)
        with self._queue_lock:
            existing = self._queued_followup
            if existing is None:
                self._queued_followup = request
            else:
                # Coalesce. A full-scan dominates targeted updates.
                if request["paths"] is None or existing.get("paths") is None:
                    self._queued_followup = {
                        "trigger": request["trigger"] if request["paths"] is None else existing["trigger"],
                        "paths": None,
                    }
                else:
                    merged_paths = existing["paths"] | request["paths"]
                    if len(merged_paths) > _TARGETED_PROMOTE_THRESHOLD:
                        # Too many distinct paths queued — promote to a full
                        # scan instead of holding the set in memory.
                        self._queued_followup = {"trigger": request["trigger"], "paths": None}
                    else:
                        self._queued_followup = {"trigger": request["trigger"], "paths": merged_paths}
        self._dispatcher_wake.set()

    # ------------------------------------------------------------------
    # Listener registration (sync — FastAPI bridges to async)
    # ------------------------------------------------------------------
    def add_listener(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        with self._listeners_lock:
            self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._listeners_lock:
                try:
                    self._listeners.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def _broadcast(self, terminal: bool = False) -> None:
        snap = self.progress.snapshot()
        snap["_terminal"] = terminal
        with self._listeners_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(snap)
            except Exception:  # pragma: no cover
                logger.exception("listener callback raised")

    # ------------------------------------------------------------------
    # Dispatcher loop
    # ------------------------------------------------------------------
    def _dispatcher_loop(self) -> None:
        while not self._stopping.is_set():
            self._dispatcher_wake.wait()
            self._dispatcher_wake.clear()
            while not self._stopping.is_set():
                with self._queue_lock:
                    req = self._queued_followup
                    self._queued_followup = None
                if req is None:
                    break
                with self._run_lock:
                    try:
                        self._execute(req)
                    except Exception:  # pragma: no cover
                        logger.exception("index run crashed")

    # ------------------------------------------------------------------
    # One run
    # ------------------------------------------------------------------
    def _execute(self, request: dict) -> None:
        trigger: str = request["trigger"]
        paths_filter: Optional[set[Path]] = request["paths"]
        started_at = _utcnow_iso()
        t0 = time.monotonic()

        path_info = "full" if paths_filter is None else f"{len(paths_filter)} paths"
        logger.info("run start: trigger=%s %s", trigger, path_info)
        self.progress.reset(trigger=trigger, started_at=started_at)
        self._broadcast()

        writer = _Writer(self.db_path, self.write_batch)
        writer.start()
        run_id = writer.submit_run_start(trigger, started_at)

        status = "done"
        try:
            if paths_filter is None:
                self._run_full_scan(writer)
            else:
                self._run_targeted(writer, paths_filter)
        except Exception:
            logger.exception("indexing run failed")
            status = "error"
        finally:
            writer.stop()  # drains queue, closes connection

        ended_at = _utcnow_iso()
        duration = round(time.monotonic() - t0, 3)
        snap = self.progress.snapshot()
        last_run = {
            "id": run_id,
            "trigger": trigger,
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": duration,
            "added": snap["added"],
            "updated": snap["updated"],
            "removed": snap["removed"],
            "skipped": snap["skipped"],
            "error_count": len(snap["errors"]),
        }
        # Finalize the index_runs row in its own short-lived writer.
        _finalize_run(self.db_path, run_id, status, ended_at, duration, snap, len(snap["errors"]))
        self.progress.finalize(status=status, last_run=last_run)
        logger.info(
            "run done: trigger=%s status=%s duration=%.2fs added=%d updated=%d removed=%d skipped=%d errors=%d",
            trigger, status, duration, snap["added"], snap["updated"], snap["removed"], snap["skipped"], len(snap["errors"]),
        )
        self._broadcast(terminal=True)

    # -- full scan ------------------------------------------------------
    def _run_full_scan(self, writer: "_Writer") -> None:
        all_paths = list(iter_ebook_files(self.ebook_dir))
        self.progress.set_discovered(len(all_paths))
        self._broadcast()

        # Load existing fingerprints for skip detection + dead-row deletion.
        existing = _load_fingerprints(self.db_path)
        existing_paths = set(existing.keys())
        seen_paths: set[str] = set()

        to_parse: list[Path] = []
        for p in all_paths:
            sp = str(p)
            seen_paths.add(sp)
            try:
                stat = p.stat()
            except OSError as exc:
                self.progress.note_error(sp, f"stat failed: {exc}")
                self._broadcast()
                continue
            if self.max_file_bytes and stat.st_size > self.max_file_bytes:
                self.progress.note_error(sp, f"skipped: {stat.st_size} bytes exceeds limit of {self.max_file_bytes}")
                continue
            prev = existing.get(sp)
            if prev and prev[0] == stat.st_mtime and prev[1] == stat.st_size:
                self.progress.note_processed(current_file=sp, outcome="skipped")
                continue
            to_parse.append(p)

        self._parse_and_write(writer, to_parse)

        # Delete rows for files that disappeared.
        removed = existing_paths - seen_paths
        for path in removed:
            writer.submit_delete(path)
            self.progress.note_processed(current_file=path, outcome="removed")
        if removed:
            self._broadcast()

    # -- targeted -------------------------------------------------------
    def _run_targeted(self, writer: "_Writer", paths: set[Path]) -> None:
        existing_alive: list[Path] = []
        alive_stats: dict[Path, tuple[float, int]] = {}
        dead: list[Path] = []
        for p in paths:
            if p.exists() and is_ebook_file(p):
                try:
                    st = p.stat()
                except OSError as exc:
                    self.progress.note_error(str(p), f"stat failed: {exc}")
                    continue
                if self.max_file_bytes and st.st_size > self.max_file_bytes:
                    self.progress.note_error(str(p), f"skipped: {st.st_size} bytes exceeds limit of {self.max_file_bytes}")
                    continue
                existing_alive.append(p)
                alive_stats[p] = (st.st_mtime, st.st_size)
            else:
                dead.append(p)

        # Skip files whose (mtime, size) matches the DB — without this, a
        # phantom watchdog event would re-parse every reported file and (with
        # spurious events on network filesystems) cause perpetual re-scans.
        fingerprints = _load_fingerprints_for(self.db_path, [str(p) for p in existing_alive])
        to_parse: list[Path] = []
        unchanged_count = 0
        for p in existing_alive:
            prev = fingerprints.get(str(p))
            mt, sz = alive_stats[p]
            if prev and prev[0] == mt and prev[1] == sz:
                self.progress.note_processed(current_file=str(p), outcome="skipped")
                unchanged_count += 1
            else:
                to_parse.append(p)

        if unchanged_count:
            logger.info(
                "targeted run: %d unchanged (skipped), %d to parse, %d to delete",
                unchanged_count, len(to_parse), len(dead),
            )

        self.progress.set_discovered(len(existing_alive) + len(dead))
        self._broadcast()
        # Upserts must run before deletes so move detection can see the
        # still-alive old-path row when a rename pairs a created+deleted event.
        self._parse_and_write(writer, to_parse)
        for p in dead:
            writer.submit_delete(str(p))
            self.progress.note_processed(current_file=str(p), outcome="removed")
        if dead:
            self._broadcast()

    # -- shared parse/write loop ---------------------------------------
    def _parse_and_write(self, writer: "_Writer", paths: list[Path]) -> None:
        if not paths:
            return
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="parse") as pool:
            futures = {pool.submit(extract, p, self.ebook_dir): p for p in paths}
            last_emit = 0.0
            for fut in as_completed(futures):
                path = futures[fut]
                try:
                    parsed: ParsedBook = fut.result()
                except Exception as exc:
                    self.progress.note_error(str(path), str(exc))
                    self._maybe_emit(last_emit)
                    last_emit = time.monotonic()
                    continue
                outcome = writer.submit_upsert(parsed)  # returns desired progress label
                self.progress.note_processed(current_file=parsed.path, outcome=outcome)
                now = time.monotonic()
                if now - last_emit >= 0.25:
                    self._broadcast()
                    last_emit = now
        self._broadcast()

    def _maybe_emit(self, last_emit: float) -> None:
        if time.monotonic() - last_emit >= 0.25:
            self._broadcast()


# ----------------------------------------------------------------------
# Writer thread
# ----------------------------------------------------------------------

class _Writer:
    """Owns the single write connection. Runs in its own thread.

    Submission methods are called from parse-pool threads; they classify each
    parse result (added / updated / moved) via a short read-only SQLite query
    — WAL keeps these concurrent with the writer at negligible cost — then
    enqueue the operation. The writer drains the queue in batched transactions.
    """

    def __init__(self, db_path: Path, batch_size: int) -> None:
        self.db_path = db_path
        self.batch_size = batch_size
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="index-writer", daemon=True)
        # Thread-local read connections so parse-pool workers don't pay the
        # open/close cost on every upsert. Each opened connection is also
        # tracked in _readers so stop() can close them deterministically.
        self._tls = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._q.put(_STOP)
        self._thread.join(timeout=30)
        # Safe to close all reader connections now: stop() is only called
        # after _parse_and_write's ThreadPoolExecutor has exited and joined
        # its workers, so no thread is using these connections any more.
        with self._readers_lock:
            for c in self._readers:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._readers.clear()

    def _reader(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = dbmod.connect(self.db_path, read_only=True)
            self._tls.conn = conn
            with self._readers_lock:
                self._readers.append(conn)
        return conn

    # --- submission API ------------------------------------------------
    def submit_upsert(self, parsed: ParsedBook) -> str:
        conn = self._reader()
        # 1. Path already in the DB → in-place update.
        row = conn.execute("SELECT 1 FROM books WHERE path = ? LIMIT 1", (parsed.path,)).fetchone()
        if row is not None:
            self._q.put(("upsert", parsed))
            return "updated"

        # 2. New path. Try to recognize a rename: a row with the same
        #    content_hash whose path is no longer on disk.
        if parsed.content_hash:
            candidate = conn.execute(
                "SELECT path FROM books WHERE content_hash = ? AND path != ? LIMIT 1",
                (parsed.content_hash, parsed.path),
            ).fetchone()
            if candidate is not None:
                old_path = candidate["path"]
                try:
                    gone = not Path(old_path).exists()
                except OSError:
                    gone = True
                if gone:
                    self._q.put(("move", parsed, old_path))
                    return "updated"

        # 3. Genuinely new row.
        self._q.put(("upsert", parsed))
        return "added"

    def submit_delete(self, path: str) -> None:
        self._q.put(("delete", path))

    def submit_run_start(self, trigger: str, started_at: str) -> int:
        """Insert the index_runs row synchronously and return its id."""
        conn = dbmod.connect(self.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO index_runs(trigger, status, started_at) VALUES (?, 'running', ?)",
                (trigger, started_at),
            )
            return int(cur.lastrowid)
        finally:
            conn.close()

    # --- thread main ---------------------------------------------------
    def _run(self) -> None:
        conn = dbmod.connect(self.db_path)
        try:
            pending = 0
            last_commit = time.monotonic()
            conn.execute("BEGIN")
            while True:
                try:
                    item = self._q.get(timeout=1.0)
                except queue.Empty:
                    if pending > 0 and time.monotonic() - last_commit >= 1.0:
                        conn.execute("COMMIT")
                        conn.execute("BEGIN")
                        pending = 0
                        last_commit = time.monotonic()
                    continue
                if item is _STOP:
                    break
                op = item[0]
                try:
                    if op == "upsert":
                        _upsert(conn, item[1])
                    elif op == "move":
                        # If the move-target row vanished between submit and apply
                        # (concurrent delete), fall back to a regular upsert so
                        # the new file still ends up in the index.
                        if _move(conn, item[1], item[2]) == 0:
                            _upsert(conn, item[1])
                    elif op == "delete":
                        conn.execute("DELETE FROM books WHERE path = ?", (item[1],))
                except sqlite3.Error as exc:
                    logger.exception("writer SQL error: %s", exc)
                pending += 1
                if pending >= self.batch_size:
                    conn.execute("COMMIT")
                    conn.execute("BEGIN")
                    pending = 0
                    last_commit = time.monotonic()
            # Final commit
            try:
                conn.execute("COMMIT")
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()


def _upsert(conn: sqlite3.Connection, p: ParsedBook) -> None:
    conn.execute(
        """
        INSERT INTO books(path, rel_path, filename, ext, size_bytes, mtime,
                          content_hash, title, author, language, publisher,
                          pub_date, page_count, indexed_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            rel_path     = excluded.rel_path,
            filename     = excluded.filename,
            ext          = excluded.ext,
            size_bytes   = excluded.size_bytes,
            mtime        = excluded.mtime,
            content_hash = excluded.content_hash,
            title        = excluded.title,
            author       = excluded.author,
            language     = excluded.language,
            publisher    = excluded.publisher,
            pub_date     = excluded.pub_date,
            page_count   = excluded.page_count,
            indexed_at   = excluded.indexed_at
        """,
        (
            p.path,
            p.rel_path,
            p.filename,
            p.ext,
            p.size_bytes,
            p.mtime,
            p.content_hash,
            p.title,
            p.author,
            p.language,
            p.publisher,
            p.pub_date,
            p.page_count,
            _utcnow_iso(),
        ),
    )


def _move(conn: sqlite3.Connection, p: ParsedBook, old_path: str) -> int:
    """Rewrite the row whose ``path == old_path`` to use the new path/metadata.

    Returns the number of rows affected (0 if the source row vanished, in
    which case the caller should fall back to a regular insert).
    """
    cur = conn.execute(
        """
        UPDATE books SET
            path         = ?,
            rel_path     = ?,
            filename     = ?,
            ext          = ?,
            size_bytes   = ?,
            mtime        = ?,
            content_hash = ?,
            title        = ?,
            author       = ?,
            language     = ?,
            publisher    = ?,
            pub_date     = ?,
            page_count   = ?,
            indexed_at   = ?
        WHERE path = ?
        """,
        (
            p.path,
            p.rel_path,
            p.filename,
            p.ext,
            p.size_bytes,
            p.mtime,
            p.content_hash,
            p.title,
            p.author,
            p.language,
            p.publisher,
            p.pub_date,
            p.page_count,
            _utcnow_iso(),
            old_path,
        ),
    )
    return cur.rowcount


def _load_fingerprints(db_path: Path) -> dict[str, tuple[float, int]]:
    conn = dbmod.connect(db_path, read_only=True)
    try:
        return {row["path"]: (row["mtime"], row["size_bytes"]) for row in conn.execute("SELECT path, mtime, size_bytes FROM books")}
    finally:
        conn.close()


def _load_fingerprints_for(db_path: Path, paths: list[str]) -> dict[str, tuple[float, int]]:
    """Like ``_load_fingerprints`` but only for the given paths."""
    if not paths:
        return {}
    conn = dbmod.connect(db_path, read_only=True)
    try:
        # Chunk to keep parameter list under SQLite's variable limit (999).
        result: dict[str, tuple[float, int]] = {}
        for i in range(0, len(paths), 500):
            chunk = paths[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            cur = conn.execute(
                f"SELECT path, mtime, size_bytes FROM books WHERE path IN ({placeholders})",
                chunk,
            )
            for row in cur:
                result[row["path"]] = (row["mtime"], row["size_bytes"])
        return result
    finally:
        conn.close()


def _finalize_run(
    db_path: Path,
    run_id: int,
    status: str,
    ended_at: str,
    duration: float,
    snap: dict,
    error_count: int,
) -> None:
    conn = dbmod.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE index_runs
               SET status = ?, ended_at = ?, duration_seconds = ?,
                   added = ?, updated = ?, removed = ?, skipped = ?, error_count = ?
             WHERE id = ?
            """,
            (
                status,
                ended_at,
                duration,
                snap["added"],
                snap["updated"],
                snap["removed"],
                snap["skipped"],
                error_count,
                run_id,
            ),
        )
    finally:
        conn.close()
