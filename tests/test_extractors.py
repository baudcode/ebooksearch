from pathlib import Path

from ebooksearch.extractors import extract, is_ebook_file

from conftest import make_epub, make_pdf


def test_epub_metadata(ebook_env):
    p = make_epub(ebook_env["ebook_dir"] / "tale.epub", title="A Tale", author="Charles D.")
    parsed = extract(p, ebook_env["ebook_dir"])
    assert parsed.title == "A Tale"
    assert "Charles D." in (parsed.author or "")
    assert parsed.ext == ".epub"
    assert parsed.language == "en"
    assert parsed.size_bytes > 0


def test_pdf_metadata(ebook_env):
    p = make_pdf(ebook_env["ebook_dir"] / "doc.pdf", title="A PDF", author="John PDF")
    parsed = extract(p, ebook_env["ebook_dir"])
    assert parsed.title == "A PDF"
    assert parsed.author == "John PDF"
    assert parsed.ext == ".pdf"
    assert parsed.page_count == 1


def test_flatten_toc_handles_link_and_nested():
    """ebooklib's toc can be Link / list / nested tuple. Must not crash."""
    from ebooksearch.extractors import _flatten_toc

    class FakeLink:
        def __init__(self, title): self.title = title

    out: list[dict] = []
    _flatten_toc(None, out)
    assert out == []

    out = []
    _flatten_toc(FakeLink("Solo"), out)
    assert out == [{"title": "Solo"}]

    out = []
    _flatten_toc([FakeLink("A"), FakeLink("B")], out)
    assert out == [{"title": "A"}, {"title": "B"}]

    out = []
    _flatten_toc((FakeLink("Section"), [FakeLink("Child1"), FakeLink("Child2")]), out)
    assert {"title": "Section"} in out
    assert {"title": "Child1"} in out
    assert {"title": "Child2"} in out


def test_is_ebook_filters(tmp_path: Path):
    assert is_ebook_file(tmp_path / "book.epub")
    assert is_ebook_file(tmp_path / "book.pdf")
    assert not is_ebook_file(tmp_path / "book.txt")
    assert not is_ebook_file(tmp_path / ".hidden.epub")
    assert not is_ebook_file(tmp_path / "book.epub.part")
