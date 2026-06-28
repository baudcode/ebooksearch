"""Populate ./test-ebooks/ with a handful of synthetic EPUB/PDF files.

Run: ``python scripts/build_fixtures.py``
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from conftest import make_epub, make_pdf  # noqa: E402


def main() -> None:
    out = ROOT / "test-ebooks"
    out.mkdir(exist_ok=True)
    make_epub(out / "the-great-gatsby.epub", title="The Great Gatsby", author="F. Scott Fitzgerald", publisher="Scribner", date="1925")
    make_epub(out / "moby-dick.epub", title="Moby-Dick", author="Herman Melville", publisher="Harper", date="1851")
    make_epub(out / "frankenstein.epub", title="Frankenstein", author="Mary Shelley", publisher="Lackington", date="1818")
    make_pdf(out / "linear-algebra-notes.pdf", title="Linear Algebra Notes", author="Course Staff")
    make_pdf(out / "intro-to-databases.pdf", title="Intro to Databases", author="DB Author")
    print(f"wrote {len(list(out.glob('*')))} files to {out}")


if __name__ == "__main__":
    main()
