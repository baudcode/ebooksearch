"""FastAPI app — routes, SSE bridge, lifespan."""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

# Configure logging at import time so INFO from our modules shows up alongside
# uvicorn's access logs in container output. Override with LOG_LEVEL=DEBUG etc.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import lite as litemod
from . import search as searchmod
from .config import Config
from .extractors import preview
from .indexer import IndexManager
from .watcher import FolderWatcher

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Preview cache keyed by content_hash → (snippet, toc)
_PREVIEW_CACHE: dict[str, tuple[str, list[dict]]] = {}
_PREVIEW_CACHE_LIMIT = 128


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = Config.from_env()
    cfg.ebook_dir.mkdir(parents=True, exist_ok=True)
    logger.info("EBOOK_DIR=%s  DB_PATH=%s  workers=%d", cfg.ebook_dir, cfg.db_path, cfg.index_workers)

    manager = IndexManager(
        db_path=cfg.db_path,
        ebook_dir=cfg.ebook_dir,
        workers=cfg.index_workers,
        write_batch=cfg.write_batch,
        max_file_bytes=cfg.max_file_bytes,
        max_text_bytes=cfg.max_text_bytes,
    )
    manager.start()

    watcher = FolderWatcher(manager, cfg.ebook_dir, cfg.watch_debounce_seconds)
    # Watcher startup walks the tree to register inotify watches; on big
    # libraries that can take many seconds. Run it in a background thread so
    # the HTTP server starts accepting requests immediately. A short window
    # where folder events aren't caught is harmless — the startup scan that
    # follows covers everything currently on disk.
    threading.Thread(
        target=watcher.start,
        name="watcher-startup",
        daemon=True,
    ).start()

    app.state.config = cfg
    app.state.manager = manager
    app.state.watcher = watcher
    app.state.loop = asyncio.get_running_loop()

    if cfg.reindex_on_startup:
        manager.request_full_scan("startup")

    try:
        yield
    finally:
        watcher.stop()
        manager.stop()


app = FastAPI(title="ebooksearch", lifespan=lifespan)


# Old e-reader browsers (e.g. the Kindle Paperwhite) can't run the JS SPA at
# all. Send them to the server-rendered /lite page automatically. UA sniffing
# is brittle by nature, but "kindle" is a stable substring across Kindle
# browser builds and the worst case is a redirect a capable browser could undo.
@app.middleware("http")
async def kindle_to_lite(request: Request, call_next):
    if request.url.path == "/" and "kindle" in request.headers.get("user-agent", "").lower():
        return RedirectResponse(url="/lite", status_code=302)
    return await call_next(request)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/search")
async def api_search(
    request: Request,
    q: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    cfg: Config = request.app.state.config
    rows = await run_in_threadpool(searchmod.search_books, cfg.db_path, q, limit, offset)
    return {"results": rows, "limit": limit, "offset": offset, "q": q}


@app.get("/api/recent")
async def api_recent(request: Request, limit: int = Query(20, ge=1, le=200)):
    cfg: Config = request.app.state.config
    rows = await run_in_threadpool(searchmod.recent_books, cfg.db_path, limit)
    return {"results": rows}


@app.get("/api/stats")
async def api_stats(request: Request):
    cfg: Config = request.app.state.config
    return await run_in_threadpool(searchmod.stats, cfg.db_path)


@app.get("/api/index/runs")
async def api_runs(request: Request, limit: int = Query(10, ge=1, le=100)):
    cfg: Config = request.app.state.config
    rows = await run_in_threadpool(searchmod.index_runs, cfg.db_path, limit)
    return {"runs": rows}


@app.get("/api/index/runs/{run_id}/errors")
async def api_run_errors(
    request: Request,
    run_id: int,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    cfg: Config = request.app.state.config
    return await run_in_threadpool(searchmod.run_errors, cfg.db_path, run_id, limit, offset)


@app.get("/api/index/status")
async def api_index_status(request: Request):
    manager: IndexManager = request.app.state.manager
    return manager.progress.snapshot()


@app.post("/api/reindex", status_code=202)
async def api_reindex(request: Request):
    manager: IndexManager = request.app.state.manager
    manager.request_full_scan("manual")
    return {"queued": True}


@app.get("/api/book/{book_id}")
async def api_book(request: Request, book_id: int):
    cfg: Config = request.app.state.config
    book = await run_in_threadpool(searchmod.get_book, cfg.db_path, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="not found")
    snippet, toc = await run_in_threadpool(_cached_preview, book)
    book["snippet"] = snippet
    book["toc"] = toc
    return book


def _cached_preview(book: dict) -> tuple[str, list[dict]]:
    key = book.get("content_hash") or ""
    if key and key in _PREVIEW_CACHE:
        return _PREVIEW_CACHE[key]
    path = Path(book["path"])
    if not path.exists():
        return "", []
    snippet, toc = preview(path)
    if key:
        if len(_PREVIEW_CACHE) >= _PREVIEW_CACHE_LIMIT:
            _PREVIEW_CACHE.pop(next(iter(_PREVIEW_CACHE)))
        _PREVIEW_CACHE[key] = (snippet, toc)
    return snippet, toc


@app.get("/api/download/{book_id}")
async def api_download(request: Request, book_id: int):
    cfg: Config = request.app.state.config
    book = await run_in_threadpool(searchmod.get_book, cfg.db_path, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="not found")

    path = Path(book["path"]).resolve()
    root = cfg.ebook_dir.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="path outside ebook root")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="file missing")

    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        str(path),
        media_type=media_type or "application/octet-stream",
        filename=book["filename"],
        headers={"Content-Disposition": f'attachment; filename="{book["filename"]}"'},
    )


# ---------------------------------------------------------------------------
# SSE — thread → async bridge
# ---------------------------------------------------------------------------


@app.get("/api/index/stream")
async def api_index_stream(request: Request):
    manager: IndexManager = request.app.state.manager
    loop = request.app.state.loop
    queue: asyncio.Queue = asyncio.Queue(maxsize=128)

    def on_event(snapshot: dict) -> None:
        # Called from manager threads — hop onto the event loop.
        def _put() -> None:
            try:
                queue.put_nowait(snapshot)
            except asyncio.QueueFull:
                # Drop the oldest non-terminal event to keep up.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(snapshot)
                except asyncio.QueueFull:
                    pass
        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:  # pragma: no cover - loop closing
            pass

    unsubscribe = manager.add_listener(on_event)

    async def event_source():
        try:
            # Immediately send current state so the UI doesn't wait for the next tick.
            initial = manager.progress.snapshot()
            yield _sse(initial)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    snap = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(snap)
        finally:
            unsubscribe()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(snapshot: dict) -> str:
    event = "terminal" if snapshot.get("_terminal") else "progress"
    payload = {k: v for k, v in snapshot.items() if not k.startswith("_")}
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# Lite page — server-rendered, no-JS search for limited browsers
# ---------------------------------------------------------------------------


@app.get("/lite", response_class=HTMLResponse)
async def lite(request: Request, q: str = "", limit: int = Query(50, ge=1, le=200)):
    cfg: Config = request.app.state.config
    is_search = bool(q.strip())
    if is_search:
        rows = await run_in_threadpool(searchmod.search_books, cfg.db_path, q, limit, 0)
    else:
        rows = await run_in_threadpool(searchmod.recent_books, cfg.db_path, 30)
    return HTMLResponse(litemod.render_lite_page(q, rows, is_search))


# ---------------------------------------------------------------------------
# Static SPA
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
