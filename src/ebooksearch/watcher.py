"""Watchdog wiring with debounced batching → IndexManager."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .extractors import is_ebook_file
from .indexer import IndexManager

logger = logging.getLogger(__name__)


class _DebouncedHandler(FileSystemEventHandler):
    """Accumulate affected paths and flush after a quiet period."""

    def __init__(self, manager: IndexManager, debounce_seconds: float) -> None:
        self.manager = manager
        self.debounce_seconds = debounce_seconds
        self._pending: set[Path] = set()
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None

    def _interesting(self, event: FileSystemEvent) -> list[Path]:
        if event.is_directory:
            return []
        paths: list[Path] = []
        # For move events we want both src and dest considered.
        for attr in ("src_path", "dest_path"):
            val = getattr(event, attr, None)
            if val:
                p = Path(val)
                if is_ebook_file(p) or event.event_type == "deleted":
                    paths.append(p)
        return paths

    def on_any_event(self, event: FileSystemEvent) -> None:
        paths = self._interesting(event)
        if not paths:
            return
        # Detailed per-event log so users diagnosing "why is it scanning again"
        # can see exactly what the kernel reported.
        src = getattr(event, "src_path", None)
        dest = getattr(event, "dest_path", None)
        logger.info(
            "watchdog event: type=%s src=%s%s",
            event.event_type, src,
            f" dest={dest}" if dest else "",
        )
        with self._lock:
            self._pending.update(paths)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            paths = self._pending
            self._pending = set()
            self._timer = None
        if not paths:
            return
        sample = ", ".join(str(p) for p in list(paths)[:5])
        if len(paths) > 5:
            sample += f", … (+{len(paths) - 5} more)"
        logger.info("watchdog flush: %d paths [%s]", len(paths), sample)
        self.manager.request_targeted("watch", paths)


class FolderWatcher:
    def __init__(self, manager: IndexManager, root: Path, debounce_seconds: float) -> None:
        self.manager = manager
        self.root = root
        self.debounce_seconds = debounce_seconds
        self._observer: Optional[Observer] = None
        self._handler: Optional[_DebouncedHandler] = None

    def start(self) -> None:
        if not self.root.exists():
            logger.warning("watch root %s does not exist; watcher idle", self.root)
            return
        self._handler = _DebouncedHandler(self.manager, self.debounce_seconds)
        self._observer = Observer()
        self._observer.schedule(self._handler, str(self.root), recursive=True)
        self._observer.start()
        logger.info("watching %s (debounce=%.1fs)", self.root, self.debounce_seconds)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._handler and self._handler._timer is not None:
            self._handler._timer.cancel()
