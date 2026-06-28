# Local Ebook Search Server — Build Spec

Paste everything in the **Build Prompt** section below into Claude Code as a single
request. The **Locked-in Decisions** and **Future Extensions** sections at the end
are notes for you — review and flip any knob before sending.

---

## Build Prompt

Build a local ebook search server. I point it at a folder of ebooks; it indexes
filename + extracted metadata into SQLite FTS5 and serves a single-page web UI to:
full-text search, browse a compact result list, expand any row for full metadata +
a content preview, download the file, see recently indexed files, watch indexing
progress live, and see when the last reindex ran. The index auto-updates when the
folder changes, and a stats line is pinned in the header.

### Stack
- Python 3.12+, FastAPI, uvicorn[standard]
- SQLite with FTS5 (stdlib `sqlite3`, WAL mode)
- uv + `pyproject.toml`, src layout (package `ebooksearch/`)
- No frontend build step: vanilla HTML/CSS/JS served via `StaticFiles`
- Deps: `fastapi`, `uvicorn[standard]`, `ebooklib` (EPUB), `pypdf` (PDF), `watchdog`
- Run: `uv run uvicorn ebooksearch.main:app --reload`
- Config via env/CLI:
  - `EBOOK_DIR` (root, required)
  - `DB_PATH` (default `./index.db`)
  - `REINDEX_ON_STARTUP` (default `true`)
  - `INDEX_WORKERS` (default `min(8, os.cpu_count())`)
  - `WATCH_DEBOUNCE_SECONDS` (default `2.5`)
  - `WRITE_BATCH` (default `100`)

### Data Model
- Table `books`: `id` PK, `path` TEXT UNIQUE (absolute), `rel_path`, `filename`,
  `ext`, `size_bytes`, `mtime`, `content_hash` (sha256 of size + first 64KB — cheap
  change detection / cache key), `title`, `author`, `language`, `publisher`,
  `pub_date`, `page_count` (nullable), `indexed_at`.
  - `indexed_at` is bumped **only on an actual upsert**, not on skip — so the
    "recently indexed" list reflects genuinely (re)processed files.
  - Add an index on `books.indexed_at` (DESC) so the recent query is cheap.
- FTS5 **external-content** virtual table
  `books_fts(title, author, filename, content='books', content_rowid='id')` with
  INSERT/UPDATE/DELETE triggers to stay in sync (do **not** duplicate text).
  - Tokenizer: `unicode61` with `remove_diacritics=2`.
  - Rank with `bm25()`, weighting title > author > filename.
  - Default FTS scope is metadata only. Leave a clearly-marked extension point to add
    a `content` column later.
- Table `index_runs`: `id` PK, `trigger` TEXT (`startup`|`manual`|`watch`),
  `status` TEXT (`running`|`done`|`error`), `started_at`, `ended_at` (nullable while
  running), `added`, `updated`, `removed`, `skipped`, `error_count`,
  `duration_seconds` (derived). The `IndexManager` inserts a row (`status=running`)
  at run start and finalizes it (`ended_at` + counts + status) at run end. This is
  what powers "last reindex" across restarts and idle periods.

### Indexing — concurrent, single-writer (core architecture)
Implement an `IndexManager` that owns the whole indexing lifecycle:
- **Parse in parallel:** a `ThreadPoolExecutor(INDEX_WORKERS)` runs
  metadata-extraction jobs. Each job takes one path, reads + parses the file, and
  returns a `ParsedBook` or a per-file error. Jobs **never touch the database** —
  read-only parsing.
- **Write serially:** one dedicated writer thread owns the single `sqlite3` write
  connection (`check_same_thread=False`, used only from that thread). It drains a
  queue of parse results and upserts in batched transactions (commit every
  `WRITE_BATCH` rows or ~1s) to avoid per-row fsync cost. This respects SQLite's
  single-writer model while parallelizing the real bottleneck (parsing).
- **Single-flight:** only one indexing run at a time, guarded by a lock. If a run is
  requested while one is active, coalesce it (queue one follow-up) rather than
  running concurrently. Watchdog-triggered and manual reindex share this path.
- **Incremental + idempotent:** per file, skip if `(mtime, size)` unchanged; else
  parse and upsert. Full scans also delete rows whose path no longer exists.
- **Extractor registry** keyed by extension (so new formats are trivial):
  - EPUB via `ebooklib`: `dc:title`, `dc:creator`→author, `dc:language`,
    `dc:publisher`, `dc:date`.
  - PDF via `pypdf`: document info / XMP; `page_count`.
  - Fallback: derive title from filename when metadata missing.
- Searches stay responsive **during** indexing — WAL lets readers run concurrently
  with the single writer. Verify this works.

### Progress Monitoring (live)
- `IndexManager` holds a thread-safe `ProgressState`: `status` (`idle`|`scanning`|
  `indexing`|`done`|`error`), `total_discovered`, `processed`, `added`, `updated`,
  `removed`, `skipped`, `errors` (path+message list, capped), `current_file`,
  `started_at`, `files_per_sec` (rolling window), `eta_seconds`.
- `ProgressState` snapshot includes a `last_run` object when idle: `{trigger, status,
  started_at, ended_at, duration_seconds, added, updated, removed, skipped,
  error_count}` — so the idle banner needs no extra round-trip.
- `GET /api/index/status` → snapshot of `ProgressState` (polling fallback).
- `GET /api/index/stream` → Server-Sent Events pushing `ProgressState` on each
  update (or ~250ms throttle). **Subtle part:** the writer/worker threads are sync
  but SSE is async — bridge them correctly (e.g. an `asyncio.Queue` fed via
  `loop.call_soon_threadsafe` / `run_coroutine_threadsafe` captured at startup). Do
  not block the event loop, and do not lose the final terminal event. The terminal
  `done`/`error` event **must carry the finalized `index_runs` record** so the UI can
  update "last reindex" and refresh "recently indexed" without polling.
- `POST /api/reindex` → triggers a full scan, returns immediately (`202`) with the
  run starting in the background; progress is observed via the stream/status.

### Auto-Reindex on Folder Changes (watchdog)
- `watchdog` `Observer` watches `EBOOK_DIR` recursively, started/stopped via FastAPI
  lifespan.
- **Debounce:** file events arrive in bursts (large copies emit many modified
  events; editors write temp files). Accumulate affected paths into a set and only
  act after `WATCH_DEBOUNCE_SECONDS` of quiet.
- **Targeted updates:** handle created/modified/moved/deleted by indexing or removing
  just the affected paths — not a full rescan. Route through `IndexManager` so the
  single-writer + single-flight guarantees still hold, and progress still streams.
- Ignore non-ebook extensions, hidden/dotfiles, and in-progress temp files
  (`.part`, `.crdownload`, `.tmp`, `~$*`).

### Lazy Content Preview (not stored in FTS)
- `GET /api/book/{id}`: full metadata + a text snippet (~1500 chars of extracted
  text), TOC/chapter list if available (EPUB nav/spine), `page_count` for PDF.
  Extract on demand; cache by `content_hash`. Optional: extract cover image as a
  base64 thumbnail.

### Search + Download
- `GET /api/search?q=&limit=&offset=`: escape user input into a safe FTS query
  (support prefix via trailing `*`). Return ranked compact rows `{id, title, author,
  filename, ext, size_bytes}`. Empty `q` → recent/all, paginated.
- `GET /api/download/{id}`: `FileResponse` with correct media type and
  `Content-Disposition: attachment`. **Critical:** resolve the stored path and verify
  it is inside `EBOOK_DIR` (reject traversal); `404` if missing.

### Recent Files, Run History, and Stats (API)
- `GET /api/recent?limit=` (default 20): most recently indexed books, ordered by
  `indexed_at` DESC. Return compact rows `{id, title, author, filename, ext,
  size_bytes, indexed_at}` — same shape as search rows plus `indexed_at`, so they
  reuse the accordion component.
- `GET /api/index/runs?limit=` (default 10): recent `index_runs` rows, newest first.
  UI uses `[0]` for the "last reindex" display; the rest is an optional history list.
- `GET /api/stats`: `{total_books, db_size_bytes, last_run}` where `total_books` is a
  single `SELECT count(*) FROM books`, `db_size_bytes` is the size of `DB_PATH` on
  disk (sum the `-wal`/`-shm` sidecars if present), and `last_run` is the latest
  `index_runs` row (timestamp + summary). Cheap to compute; safe to call on every
  load and after each terminal stream event.

### Frontend (static SPA, no framework, served at `/`)
- **Header stats line (pinned):** total books indexed, DB size (human-readable), and
  last-reindex timestamp (relative, e.g. "12 min ago", with absolute on hover).
  Sourced from `/api/stats`; refreshes on load and whenever the SSE stream emits a
  terminal event.
- **Search:** debounced search box. Results as a compact accordion list — one line
  per book (`title — author`, small `ext` + human-readable size badge). Expanding a
  row lazily fetches `/api/book/{id}` and shows full metadata, snippet, TOC, and a
  Download button. Keyboard navigable.
- **Recently indexed panel:** shown when the search box is empty (or as a
  collapsible section). Compact accordion list from `/api/recent`, each item showing
  a relative timestamp (absolute on hover). Expands and downloads exactly like a
  search result. Re-fetches when the SSE stream emits a terminal event.
- **Indexing UI:** a status banner/progress bar driven by `EventSource` on
  `/api/index/stream` — shows phase, processed/total, a progress bar, current file,
  files/sec, ETA, and an error count (expandable). A "Reindex now" button hits
  `/api/reindex`. Banner appears during runs and on folder-change activity, and
  collapses when idle.
- **Last reindex (idle state):** when idle, the banner shows the last run — trigger
  label (Startup / Manual / Folder change), started + ended timestamps, duration,
  and a one-line summary (`+N added, ~N updated, −N removed, N errors`). While a run
  is active it shows live progress instead; on completion it flips back to this
  summary, populated from the terminal stream event.
- Minimal, clean CSS. API lives under `/api`, static UI at `/`. Timestamps stored
  UTC; rendered in local time + relative form.

### Quality Bar
- Async handlers; wrap blocking file/SQLite work with `run_in_threadpool` where it's
  on the request path.
- Clean shutdown: stop observer, drain writer queue, close connections.
- Type hints throughout.
- Graceful per-file error isolation (one bad file never aborts a run).
- Short README with setup + run commands.
- One coherent codebase, no over-engineering.

### Future Extensions (build the seams now, don't implement yet)
- **`sqlite-vec` semantic search:** leave a clearly-marked extension point — a place
  to add an embeddings table + ANN query path and a "search mode" toggle in the UI —
  so it can be added later without restructuring.
- **Full-text-of-contents search:** the `content` column hook in `books_fts`.
- **Additional formats (MOBI/AZW3):** via the extractor registry.

---

## Locked-in Decisions (flip before sending if you disagree)

- **Threads, not processes, for parsing.** As requested. Caveat: EPUB/PDF parsing is
  partly pure-Python and GIL-bound, so throughput scales with workers mainly because
  file I/O and some C parsing release the GIL — not linearly. Clean escalation if it
  becomes CPU-bound: swap the parse pool for a `ProcessPoolExecutor`; the
  single-writer design already isolates DB writes, so only the parse stage changes.
- **SSE, not WebSockets, for progress.** One-way stream, works with plain
  `EventSource`, no extra deps. The thread→async bridge is the only fiddly bit and is
  flagged explicitly so it isn't botched.
- **Targeted updates on folder changes**, not full rescans — far faster for a single
  dropped-in file, at the cost of slightly more event-handling logic.
- **Every run is recorded, including watchdog-triggered ones**, with `trigger`
  distinguishing them — so "last reindex" reflects the most recent activity of any
  kind. If you'd rather "last reindex" mean only full/manual scans: *the
  Last-reindex display shows the most recent run where `trigger ≠ watch`; watch runs
  still appear in the history list.*
- **Reused the accordion component** for recent files rather than a separate widget —
  one expand/download code path, consistent UX.
- **FTS scope = metadata only** (filename + author + title); content stays a lazy
  preview plus a marked extension point. Change the FTS table to include `content` if
  you want full-text-of-contents searchable from day one.
- **Formats = EPUB + PDF.** MOBI/AZW3 left out (messier; need `mobi`/Calibre), but
  the extractor registry makes adding them a small change.
- **stdlib `sqlite3` + threadpool** rather than `aiosqlite`. Fewer deps, fine for
  local read-heavy load. Swap if you want fully async DB access.

## Optional Next Add-on
A tiny **test fixture** — a few synthetic EPUB/PDFs plus a pytest that asserts
index → search → download, and a change-triggers-reindex case. On a concurrency-heavy
build like this, that's the single thing that saves the most debugging time.
