"""Test fixtures: synthetic EPUB/PDF generation."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# EPUB — a minimal valid EPUB3 archive built by hand.
# ---------------------------------------------------------------------------

_CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

_OPF_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">test-{slug}</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>{lang}</dc:language>
    <dc:publisher>{publisher}</dc:publisher>
    <dc:date>{date}</dc:date>
    <meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>
"""

_NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>nav</title></head><body><nav epub:type="toc"><ol><li><a href="ch1.xhtml">Chapter 1</a></li></ol></nav></body></html>
"""

_CH_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ch</title></head>
<body><h1>Chapter 1</h1><p>{body}</p></body></html>
"""


def make_epub(
    path: Path,
    *,
    title: str = "Test Book",
    author: str = "Jane Doe",
    lang: str = "en",
    publisher: str = "Acme",
    date: str = "2024",
    body: str = "The quick brown fox jumps over the lazy dog. " * 12,
) -> Path:
    slug = path.stem
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", _CONTAINER_XML)
        z.writestr("OEBPS/content.opf", _OPF_TMPL.format(slug=slug, title=title, author=author, lang=lang, publisher=publisher, date=date))
        z.writestr("OEBPS/nav.xhtml", _NAV)
        z.writestr("OEBPS/ch1.xhtml", _CH_TMPL.format(body=body))
    return path


# ---------------------------------------------------------------------------
# PDF — a tiny hand-rolled single-page PDF with title metadata.
# ---------------------------------------------------------------------------

def make_pdf(path: Path, *, title: str = "Test PDF", author: str = "John PDF") -> Path:
    # Smallest viable PDF; structure follows the PDF 1.4 spec minimum.
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length 44 >>\nstream\nBT /F1 12 Tf 20 100 Td (Hello PDF) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Title ({title}) /Author ({author}) >>".encode("latin-1"),
    ]
    body = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(body)
    body += f"xref\n0 {len(objects)+1}\n".encode()
    body += b"0000000000 65535 f \n"
    for off in offsets:
        body += f"{off:010d} 00000 n \n".encode()
    body += f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    path.write_bytes(body)
    return path


# ---------------------------------------------------------------------------
# Pytest fixture: empty ebook directory + db path under tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture()
def ebook_env(tmp_path: Path) -> dict:
    ebook_dir = tmp_path / "books"
    ebook_dir.mkdir()
    db_path = tmp_path / "index.db"
    return {"ebook_dir": ebook_dir, "db_path": db_path, "tmp": tmp_path}
