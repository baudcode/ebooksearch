"""Per-format metadata + content extractors.

Each extractor is a function ``(path: Path) -> ParsedBook``. The registry maps
file extensions to extractors. To support a new format (e.g. MOBI/AZW3), write
an extractor and register it here — no other code changes needed.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Callable, Optional

from .models import ParsedBook

logger = logging.getLogger(__name__)

EBOOK_EXTENSIONS = {".epub", ".pdf"}
IGNORED_SUFFIXES = (".part", ".crdownload", ".tmp")


def is_ebook_file(path: Path) -> bool:
    """True for files we should attempt to index."""
    name = path.name
    if not name or name.startswith(".") or name.startswith("~$"):
        return False
    if name.endswith(IGNORED_SUFFIXES):
        return False
    return path.suffix.lower() in EBOOK_EXTENSIONS


def content_hash(path: Path, size: int) -> str:
    """Cheap change-detection hash: sha256(size + first 64 KiB)."""
    h = hashlib.sha256()
    h.update(str(size).encode())
    try:
        with path.open("rb") as f:
            h.update(f.read(64 * 1024))
    except OSError:
        return ""
    return h.hexdigest()


def _base_parsed(path: Path, root: Path) -> ParsedBook:
    stat = path.stat()
    return ParsedBook(
        path=str(path),
        rel_path=str(path.relative_to(root)) if path.is_relative_to(root) else path.name,
        filename=path.name,
        ext=path.suffix.lower(),
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
        content_hash=content_hash(path, stat.st_size),
        title=path.stem,
    )


def _coerce_str(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace").strip() or None
        except Exception:
            return None
    s = str(value).strip()
    return s or None


def _extract_epub(path: Path, root: Path) -> ParsedBook:
    book = _base_parsed(path, root)
    try:
        from ebooklib import epub
    except ImportError:  # pragma: no cover
        return book

    try:
        ebook = epub.read_epub(str(path), options={"ignore_ncx": True})
    except Exception as exc:
        logger.debug("epub read failed for %s: %s", path, exc)
        return book

    def _meta(ns: str, name: str) -> Optional[str]:
        items = ebook.get_metadata(ns, name)
        if not items:
            return None
        value, _attrs = items[0]
        return _coerce_str(value)

    book.title = _meta("DC", "title") or book.title
    authors = ebook.get_metadata("DC", "creator")
    if authors:
        names = [_coerce_str(v) for v, _ in authors]
        book.author = ", ".join(n for n in names if n) or None
    book.language = _meta("DC", "language")
    book.publisher = _meta("DC", "publisher")
    book.pub_date = _meta("DC", "date")
    return book


def _extract_pdf(path: Path, root: Path) -> ParsedBook:
    book = _base_parsed(path, root)
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return book

    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as exc:
        logger.debug("pdf open failed for %s: %s", path, exc)
        return book

    try:
        book.page_count = len(reader.pages)
    except Exception:
        book.page_count = None

    info = getattr(reader, "metadata", None)
    if info:
        title = _coerce_str(info.get("/Title"))
        author = _coerce_str(info.get("/Author"))
        if title:
            book.title = title
        if author:
            book.author = author

    # XMP fallback for fields document info doesn't have
    try:
        xmp = reader.xmp_metadata
    except Exception:
        xmp = None
    if xmp is not None:
        try:
            if not book.language and getattr(xmp, "dc_language", None):
                lang = xmp.dc_language
                book.language = _coerce_str(lang[0] if isinstance(lang, list) and lang else lang)
        except Exception:
            pass
        try:
            if not book.publisher and getattr(xmp, "dc_publisher", None):
                pub = xmp.dc_publisher
                book.publisher = _coerce_str(pub[0] if isinstance(pub, list) and pub else pub)
        except Exception:
            pass

    return book


_REGISTRY: dict[str, Callable[[Path, Path], ParsedBook]] = {
    ".epub": _extract_epub,
    ".pdf": _extract_pdf,
}


def extract(path: Path, root: Path) -> ParsedBook:
    """Dispatch to the registered extractor for ``path.suffix``."""
    extractor = _REGISTRY.get(path.suffix.lower())
    if extractor is None:
        return _base_parsed(path, root)
    return extractor(path, root)


# ---------------------------------------------------------------------------
# Lazy content preview (not stored — extracted on demand by /api/book/{id})
# ---------------------------------------------------------------------------

PREVIEW_CHARS = 1500


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _flatten_toc(node, out: list[dict]) -> None:
    """ebooklib's ``ebook.toc`` is an irregular tree of Link / tuple / list.

    Walks it recursively and appends ``{"title": ...}`` for any node with a
    usable title.
    """
    if node is None:
        return
    if isinstance(node, (list, tuple)):
        for sub in node:
            _flatten_toc(sub, out)
        return
    title = _coerce_str(getattr(node, "title", None))
    if title:
        out.append({"title": title})


def preview_epub(path: Path) -> tuple[str, list[dict]]:
    try:
        from ebooklib import epub, ITEM_DOCUMENT
    except ImportError:  # pragma: no cover
        return "", []
    try:
        ebook = epub.read_epub(str(path), options={"ignore_ncx": True})
    except Exception:
        return "", []

    toc: list[dict] = []
    _flatten_toc(getattr(ebook, "toc", None), toc)

    pieces: list[str] = []
    total = 0
    for item in ebook.get_items_of_type(ITEM_DOCUMENT):
        try:
            html = item.get_content().decode("utf-8", errors="replace")
        except Exception:
            continue
        text = _clean_text(re.sub(r"<[^>]+>", " ", html))
        if not text:
            continue
        pieces.append(text)
        total += len(text)
        if total >= PREVIEW_CHARS:
            break
    snippet = _clean_text(" ".join(pieces))[:PREVIEW_CHARS]
    return snippet, toc


def preview_pdf(path: Path) -> tuple[str, list[dict]]:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return "", []
    try:
        reader = PdfReader(str(path), strict=False)
    except Exception:
        return "", []

    pieces: list[str] = []
    total = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        text = _clean_text(text)
        if not text:
            continue
        pieces.append(text)
        total += len(text)
        if total >= PREVIEW_CHARS:
            break

    toc: list[dict] = []
    try:
        outlines = reader.outline or []
        for item in outlines:
            if isinstance(item, list):
                continue
            title = getattr(item, "title", None)
            if title:
                toc.append({"title": _coerce_str(title) or ""})
    except Exception:
        pass

    return _clean_text(" ".join(pieces))[:PREVIEW_CHARS], toc


def preview(path: Path) -> tuple[str, list[dict]]:
    ext = path.suffix.lower()
    if ext == ".epub":
        return preview_epub(path)
    if ext == ".pdf":
        return preview_pdf(path)
    return "", []


def iter_ebook_files(root: Path):
    """Yield every ebook path under ``root`` (recursive)."""
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # skip dotted directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if is_ebook_file(p):
                yield p
