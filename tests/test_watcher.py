"""Watcher event filtering — _DebouncedHandler must ignore read-only events."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ebooksearch.watcher import _DebouncedHandler


class _StubManager:
    def __init__(self) -> None:
        self.targeted_calls: list[set[Path]] = []

    def request_targeted(self, trigger: str, paths) -> None:
        self.targeted_calls.append(set(paths))


def _ev(event_type: str, src: str, *, is_directory: bool = False, dest: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        event_type=event_type,
        src_path=src,
        dest_path=dest,
        is_directory=is_directory,
    )


def test_open_and_close_events_ignored(tmp_path: Path) -> None:
    """``opened`` / ``closed`` / ``closed_no_write`` must not enqueue work.

    On Linux/inotify these fire whenever any process — including the indexer
    itself parsing files — opens or closes a file under the watched tree.
    Acting on them causes a self-sustaining feedback loop for files that
    repeatedly fail to index (e.g. exceed the text cap) and so never gain a
    DB fingerprint to short-circuit subsequent runs.
    """
    mgr = _StubManager()
    h = _DebouncedHandler(mgr, debounce_seconds=0.01)
    p = str(tmp_path / "book.epub")
    for et in ("opened", "closed", "closed_no_write"):
        h.on_any_event(_ev(et, p))
    # Nothing pending — no debounce timer armed, no targeted run.
    assert h._pending == set()
    assert h._timer is None
    assert mgr.targeted_calls == []


def test_actionable_events_enqueued(tmp_path: Path) -> None:
    mgr = _StubManager()
    h = _DebouncedHandler(mgr, debounce_seconds=0.01)
    p = str(tmp_path / "book.epub")
    for et in ("created", "modified", "deleted"):
        h.on_any_event(_ev(et, p))
    assert h._pending == {Path(p)}
    # Flush via the debounce; timer is daemon so just call directly.
    h._flush()
    assert mgr.targeted_calls and mgr.targeted_calls[0] == {Path(p)}
