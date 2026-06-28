"""End-to-end HTTP smoke: spin up the app, hit search + download."""
from __future__ import annotations

import os
import time

from fastapi.testclient import TestClient

from conftest import make_epub, make_pdf


def test_http_search_and_download(ebook_env, monkeypatch):
    monkeypatch.setenv("EBOOK_DIR", str(ebook_env["ebook_dir"]))
    monkeypatch.setenv("DB_PATH", str(ebook_env["db_path"]))
    monkeypatch.setenv("REINDEX_ON_STARTUP", "true")

    make_epub(ebook_env["ebook_dir"] / "alpha.epub", title="Alpha Found", author="Sue")
    make_pdf(ebook_env["ebook_dir"] / "beta.pdf", title="Beta Found", author="Pat")

    from ebooksearch.main import app

    with TestClient(app) as client:
        # Wait for startup index to finish.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            s = client.get("/api/stats").json()
            if s["total_books"] >= 2 and s.get("last_run") and s["last_run"].get("status") == "done":
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"index never finished: {s}")

        # Search.
        r = client.get("/api/search", params={"q": "Alpha"}).json()
        assert any(row["filename"] == "alpha.epub" for row in r["results"])

        # Recent.
        r = client.get("/api/recent").json()
        ids = {row["id"] for row in r["results"]}
        assert len(ids) == 2

        # Book detail.
        book_id = next(iter(ids))
        r = client.get(f"/api/book/{book_id}")
        assert r.status_code == 200
        detail = r.json()
        assert "snippet" in detail
        assert "toc" in detail

        # Download.
        r = client.get(f"/api/download/{book_id}")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")
        assert len(r.content) > 0

        # 404 for missing.
        r = client.get("/api/book/999999")
        assert r.status_code == 404
        r = client.get("/api/download/999999")
        assert r.status_code == 404
