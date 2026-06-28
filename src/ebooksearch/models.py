"""Dataclasses shared across modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParsedBook:
    """Result of parsing one file (read-only — never touches the DB)."""

    path: str
    rel_path: str
    filename: str
    ext: str
    size_bytes: int
    mtime: float
    content_hash: str
    title: Optional[str] = None
    author: Optional[str] = None
    language: Optional[str] = None
    publisher: Optional[str] = None
    pub_date: Optional[str] = None
    page_count: Optional[int] = None


@dataclass
class ParseError:
    path: str
    message: str


@dataclass
class BookRow:
    id: int
    title: Optional[str]
    author: Optional[str]
    filename: str
    ext: str
    size_bytes: int
    indexed_at: Optional[str] = None
