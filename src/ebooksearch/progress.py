"""Thread-safe progress snapshot for live indexing UI."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ProgressState:
    """All counters guarded by the manager's lock. Mutate via IndexManager only."""

    status: str = "idle"  # idle | scanning | indexing | done | error
    trigger: Optional[str] = None
    total_discovered: int = 0
    processed: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    errors: list[dict] = field(default_factory=list)
    # Full uncapped error list — persisted to the DB at run end so the UI
    # can fetch it on demand. Not included in the SSE snapshot (would blow
    # the payload up on big libraries).
    all_errors: list[dict] = field(default_factory=list)
    dropped_errors_count: int = 0  # errors beyond MAX_ERRORS that didn't fit in the visible list
    current_file: Optional[str] = None
    started_at: Optional[str] = None
    files_per_sec: float = 0.0
    eta_seconds: Optional[float] = None
    last_run: Optional[dict] = None

    _samples: deque = field(default_factory=lambda: deque(maxlen=20), repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    MAX_ERRORS = 50

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "trigger": self.trigger,
                "total_discovered": self.total_discovered,
                "processed": self.processed,
                "added": self.added,
                "updated": self.updated,
                "removed": self.removed,
                "skipped": self.skipped,
                "errors": list(self.errors),
                "dropped_errors_count": self.dropped_errors_count,
                "current_file": self.current_file,
                "started_at": self.started_at,
                "files_per_sec": round(self.files_per_sec, 2),
                "eta_seconds": self.eta_seconds,
                "last_run": dict(self.last_run) if self.last_run else None,
            }

    def reset(self, trigger: str, started_at: str) -> None:
        with self._lock:
            self.status = "scanning"
            self.trigger = trigger
            self.total_discovered = 0
            self.processed = 0
            self.added = 0
            self.updated = 0
            self.removed = 0
            self.skipped = 0
            self.errors = []
            self.all_errors = []
            self.dropped_errors_count = 0
            self.current_file = None
            self.started_at = started_at
            self.files_per_sec = 0.0
            self.eta_seconds = None
            self._samples.clear()
            self._samples.append((time.monotonic(), 0))

    def set_discovered(self, total: int) -> None:
        with self._lock:
            self.total_discovered = total
            self.status = "indexing"

    def note_processed(self, *, current_file: Optional[str], outcome: str) -> None:
        """outcome in {added, updated, skipped, removed, error}."""
        with self._lock:
            self.processed += 1
            if outcome == "added":
                self.added += 1
            elif outcome == "updated":
                self.updated += 1
            elif outcome == "skipped":
                self.skipped += 1
            elif outcome == "removed":
                self.removed += 1
            self.current_file = current_file

            now = time.monotonic()
            self._samples.append((now, self.processed))
            if len(self._samples) >= 2:
                t0, p0 = self._samples[0]
                dt = now - t0
                if dt > 0:
                    rate = (self.processed - p0) / dt
                    self.files_per_sec = rate
                    remaining = max(0, self.total_discovered - self.processed)
                    self.eta_seconds = remaining / rate if rate > 0 else None

    def note_error(self, path: str, message: str) -> None:
        with self._lock:
            entry = {"path": path, "message": message}
            self.all_errors.append(entry)
            if len(self.errors) < self.MAX_ERRORS:
                self.errors.append(entry)
            else:
                self.dropped_errors_count += 1
            self.processed += 1
            self.current_file = path

    def drain_all_errors(self) -> list[dict]:
        """Take ownership of the full error list (called once at run end)."""
        with self._lock:
            out = self.all_errors
            self.all_errors = []
            return out

    def finalize(self, status: str, last_run: dict) -> None:
        with self._lock:
            self.status = status
            self.current_file = None
            self.last_run = last_run
