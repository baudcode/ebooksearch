"""Runtime configuration sourced from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB — raw open cap (RAM guard)
DEFAULT_MAX_TEXT_BYTES = 5 * 1024 * 1024   # 5 MiB — extractable-text cap


@dataclass(frozen=True)
class Config:
    ebook_dir: Path
    db_path: Path
    reindex_on_startup: bool
    index_workers: int
    watch_debounce_seconds: float
    write_batch: int
    max_file_bytes: int
    max_text_bytes: int

    @classmethod
    def from_env(cls) -> "Config":
        default_ebook_dir = Path(__file__).resolve().parents[2] / "test-ebooks"
        ebook_dir = Path(os.environ.get("EBOOK_DIR", str(default_ebook_dir))).expanduser().resolve()
        db_path = Path(os.environ.get("DB_PATH", "./index.db")).expanduser().resolve()
        return cls(
            ebook_dir=ebook_dir,
            db_path=db_path,
            reindex_on_startup=_bool("REINDEX_ON_STARTUP", True),
            index_workers=_int("INDEX_WORKERS", min(8, os.cpu_count() or 4)),
            watch_debounce_seconds=_float("WATCH_DEBOUNCE_SECONDS", 2.5),
            write_batch=_int("WRITE_BATCH", 100),
            max_file_bytes=_int("MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES),
            max_text_bytes=_int("MAX_TEXT_BYTES", DEFAULT_MAX_TEXT_BYTES),
        )
