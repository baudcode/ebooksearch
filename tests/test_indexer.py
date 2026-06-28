"""Indexer + search smoke test: build → search → reindex on change."""
from __future__ import annotations

import threading
import time

import ebooksearch.indexer as indexer_mod
from ebooksearch.indexer import IndexManager
from ebooksearch.search import search_books, recent_books, stats

from conftest import make_epub, make_pdf


def _wait_idle(manager: IndexManager, timeout: float = 10.0) -> None:
    """Block until at least one run has completed and the dispatcher is idle."""
    done = threading.Event()
    seen_terminal = {"v": False}

    def listener(snap: dict) -> None:
        if snap.get("_terminal"):
            seen_terminal["v"] = True
            done.set()

    unsub = manager.add_listener(listener)
    try:
        if not done.wait(timeout=timeout):
            raise TimeoutError("indexing did not finish within timeout")
    finally:
        unsub()
    # Brief settle so any coalesced follow-up also drains.
    deadline = time.monotonic() + timeout
    while manager.progress.snapshot()["status"] not in {"done", "error", "idle"}:
        if time.monotonic() > deadline:
            raise TimeoutError("status did not settle")
        time.sleep(0.05)


def test_index_search_and_reindex_on_change(ebook_env):
    ebook_dir = ebook_env["ebook_dir"]
    db_path = ebook_env["db_path"]

    make_epub(ebook_dir / "alpha.epub", title="Alpha Centauri", author="Sue Author")
    make_pdf(ebook_dir / "beta.pdf", title="Beta Manual", author="Pat Writer")

    mgr = IndexManager(db_path=db_path, ebook_dir=ebook_dir, workers=2, write_batch=10)
    mgr.start()
    try:
        mgr.request_full_scan("startup")
        _wait_idle(mgr)

        # Recent should show both.
        recent = recent_books(db_path, 10)
        names = {r["filename"] for r in recent}
        assert names == {"alpha.epub", "beta.pdf"}

        # Search hits title + author.
        hits = search_books(db_path, "Centauri", 10, 0)
        assert any(h["filename"] == "alpha.epub" for h in hits)
        hits = search_books(db_path, "Pat", 10, 0)
        assert any(h["filename"] == "beta.pdf" for h in hits)

        # Prefix search — both explicit `*` and bare partial tokens work,
        # because build_match_query auto-appends `*` to every token.
        hits = search_books(db_path, "alph*", 10, 0)
        assert any(h["filename"] == "alpha.epub" for h in hits)
        hits = search_books(db_path, "alph", 10, 0)
        assert any(h["filename"] == "alpha.epub" for h in hits)

        # Stats.
        s = stats(db_path)
        assert s["total_books"] == 2
        assert s["last_run"]["status"] == "done"
        assert s["last_run"]["added"] == 2

        # Targeted reindex on add: new file appears.
        new_file = ebook_dir / "gamma.epub"
        make_epub(new_file, title="Gamma Rays", author="Quanta")
        mgr.request_targeted("watch", [new_file])
        _wait_idle(mgr)

        hits = search_books(db_path, "Gamma", 10, 0)
        assert any(h["filename"] == "gamma.epub" for h in hits)

        # Targeted reindex on delete: removing the file removes the row.
        new_file.unlink()
        mgr.request_targeted("watch", [new_file])
        _wait_idle(mgr)
        names = {r["filename"] for r in recent_books(db_path, 10)}
        assert "gamma.epub" not in names
    finally:
        mgr.stop()


def test_rename_preserves_id(ebook_env):
    """Renaming a file should update the existing row rather than churning ids."""
    ebook_dir = ebook_env["ebook_dir"]
    db_path = ebook_env["db_path"]

    src = make_epub(ebook_dir / "original.epub", title="Renamed Book", author="Auth")

    mgr = IndexManager(db_path=db_path, ebook_dir=ebook_dir, workers=2, write_batch=10)
    mgr.start()
    try:
        mgr.request_full_scan("startup")
        _wait_idle(mgr)
        before = recent_books(db_path, 10)
        assert len(before) == 1
        original_id = before[0]["id"]

        # Simulate a watchdog-detected rename: both src and dest in the path set.
        dst = ebook_dir / "subdir" / "renamed.epub"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        mgr.request_targeted("watch", [src, dst])
        _wait_idle(mgr)

        after = recent_books(db_path, 10)
        assert len(after) == 1, after
        assert after[0]["id"] == original_id, "rename should preserve id"
        assert after[0]["filename"] == "renamed.epub"
    finally:
        mgr.stop()


def test_coalesced_followup_promotes_to_full_scan(ebook_env, monkeypatch):
    """A targeted queue that gets too big should collapse into a full scan."""
    monkeypatch.setattr(indexer_mod, "_TARGETED_PROMOTE_THRESHOLD", 3)

    ebook_dir = ebook_env["ebook_dir"]
    mgr = IndexManager(
        db_path=ebook_env["db_path"], ebook_dir=ebook_dir, workers=1, write_batch=10,
    )
    # Pre-fill the queued follow-up without starting the dispatcher, so we can
    # observe the coalescing logic in isolation.
    mgr._enqueue({"trigger": "watch", "paths": {ebook_dir / "a"}})
    mgr._enqueue({"trigger": "watch", "paths": {ebook_dir / "b"}})
    mgr._enqueue({"trigger": "watch", "paths": {ebook_dir / "c"}})
    mgr._enqueue({"trigger": "watch", "paths": {ebook_dir / "d"}})  # 4 > threshold of 3

    assert mgr._queued_followup is not None
    assert mgr._queued_followup["paths"] is None, "targeted set should have been promoted to full scan"
    assert mgr._queued_followup["trigger"] == "watch"


def test_writer_closes_reader_connections_on_stop(ebook_env):
    """Stop() must explicitly close all tracked reader connections."""
    from ebooksearch.indexer import _Writer

    ebook_env["db_path"].parent.mkdir(parents=True, exist_ok=True)
    # Init DB so connect() can open it.
    import sqlite3 as _sql
    from ebooksearch import db as _db
    _db.init_db(ebook_env["db_path"])

    w = _Writer(ebook_env["db_path"], batch_size=10)
    w.start()
    try:
        # Force a reader connection to be opened on this thread.
        conn = w._reader()
        assert conn in w._readers
        # Should be usable.
        conn.execute("SELECT 1").fetchone()
    finally:
        w.stop()

    assert w._readers == []
    # Connection is closed — using it should raise ProgrammingError.
    import pytest as _pytest
    with _pytest.raises(_sql.ProgrammingError):
        conn.execute("SELECT 1")


def test_targeted_skips_unchanged_files(ebook_env):
    """A watchdog event for an unchanged file must NOT re-parse it.

    This was the root cause of the perpetual-rescan loop: targeted runs used
    to upsert every reported path regardless of mtime/size.
    """
    ebook_dir = ebook_env["ebook_dir"]
    db_path = ebook_env["db_path"]

    src = make_epub(ebook_dir / "stable.epub", title="Stable Book", author="A")

    mgr = IndexManager(db_path=db_path, ebook_dir=ebook_dir, workers=1, write_batch=10)
    mgr.start()
    try:
        mgr.request_full_scan("startup")
        _wait_idle(mgr)
        initial = recent_books(db_path, 10)[0]
        first_indexed_at = initial["indexed_at"]

        # Simulate a phantom watchdog event for the same, unchanged file.
        mgr.request_targeted("watch", [src])
        _wait_idle(mgr)

        snap = mgr.progress.snapshot()
        assert snap["last_run"]["skipped"] >= 1
        assert snap["last_run"]["added"] == 0
        assert snap["last_run"]["updated"] == 0

        # indexed_at must NOT have changed (spec: bump only on actual upsert).
        after = recent_books(db_path, 10)[0]
        assert after["indexed_at"] == first_indexed_at
    finally:
        mgr.stop()


def test_oversize_file_skipped(ebook_env):
    """Files larger than max_file_bytes never reach the parse pool."""
    ebook_dir = ebook_env["ebook_dir"]
    db_path = ebook_env["db_path"]

    small = make_epub(ebook_dir / "small.epub", title="Small Book")
    big = ebook_dir / "huge.epub"
    big.write_bytes(b"\x00" * 4000)  # 4 KB
    assert big.stat().st_size == 4000

    # Limit is 2 KB — the huge file should be skipped with an error message.
    mgr = IndexManager(
        db_path=db_path, ebook_dir=ebook_dir, workers=2, write_batch=10,
        max_file_bytes=2000,
    )
    mgr.start()
    try:
        mgr.request_full_scan("startup")
        _wait_idle(mgr)

        names = {r["filename"] for r in recent_books(db_path, 10)}
        assert names == {"small.epub"}
        snap = mgr.progress.snapshot()
        oversize = [e for e in snap["errors"] if "exceeds limit" in e["message"]]
        assert any("huge.epub" in e["path"] for e in oversize), snap["errors"]
    finally:
        mgr.stop()


def test_oversize_text_skipped(ebook_env):
    """Files whose extractable text exceeds max_text_bytes are skipped, even
    if the raw file size passes the open cap. The peek must read only the zip
    central directory — no decompression — so it's safe under parallelism."""
    ebook_dir = ebook_env["ebook_dir"]
    db_path = ebook_env["db_path"]

    make_epub(ebook_dir / "small.epub", title="Small Book")
    # Body of 200 KB of repeated text → an OEBPS/ch1.xhtml entry well over the
    # 50 KB text cap we'll set on the manager, while keeping the raw zip
    # comfortably under the 1 MB open cap.
    make_epub(ebook_dir / "wordy.epub", title="Wordy Book", body="word " * 40000)

    mgr = IndexManager(
        db_path=db_path, ebook_dir=ebook_dir, workers=2, write_batch=10,
        max_file_bytes=1_000_000,
        max_text_bytes=50_000,
    )
    mgr.start()
    try:
        mgr.request_full_scan("startup")
        _wait_idle(mgr)

        names = {r["filename"] for r in recent_books(db_path, 10)}
        assert names == {"small.epub"}
        snap = mgr.progress.snapshot()
        text_errors = [e for e in snap["errors"] if "text limit" in e["message"]]
        assert any("wordy.epub" in e["path"] for e in text_errors), snap["errors"]
    finally:
        mgr.stop()


def test_full_error_list_persisted_and_queryable(ebook_env, monkeypatch):
    """All errors (incl. those past MAX_ERRORS) must be persisted per-run
    and retrievable via search.run_errors() with paging."""
    from ebooksearch.progress import ProgressState
    from ebooksearch.search import run_errors, index_runs as q_runs

    # Tiny cap so we exercise the drop-and-persist code path.
    monkeypatch.setattr(ProgressState, "MAX_ERRORS", 3)

    ebook_dir = ebook_env["ebook_dir"]
    db_path = ebook_env["db_path"]

    # 7 oversize files → 7 errors, but only 3 fit in the visible list.
    for i in range(7):
        (ebook_dir / f"big{i}.epub").write_bytes(b"\x00" * 4000)

    mgr = IndexManager(
        db_path=db_path, ebook_dir=ebook_dir, workers=2, write_batch=10,
        max_file_bytes=2000,
    )
    mgr.start()
    try:
        mgr.request_full_scan("startup")
        _wait_idle(mgr)

        snap = mgr.progress.snapshot()
        assert len(snap["errors"]) == 3  # capped
        assert snap["last_run"]["error_count"] == 7
        assert snap["last_run"]["dropped_errors_count"] == 4

        run = q_runs(db_path, 10)[0]
        all_page = run_errors(db_path, run["id"], limit=100, offset=0)
        assert all_page["total"] == 7
        assert len(all_page["errors"]) == 7

        page1 = run_errors(db_path, run["id"], limit=3, offset=0)
        page2 = run_errors(db_path, run["id"], limit=3, offset=3)
        assert len(page1["errors"]) == 3
        assert len(page2["errors"]) == 3
        # No overlap between pages.
        assert {e["path"] for e in page1["errors"]} & {e["path"] for e in page2["errors"]} == set()
    finally:
        mgr.stop()
