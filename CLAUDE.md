# CLAUDE.md

Guidance for Claude Code (or any agent) working on this repository.

## What this is

A local ebook search server. Points at a folder of `.epub` / `.pdf` files,
indexes filename + extracted metadata into SQLite FTS5, and serves a single-
page web UI for search, browse, preview, and download. The index auto-updates
when the folder changes.

Designed to run as a single long-lived container on a NAS, pointed at a
mounted ebook library.

## Stack and shape

- **Python 3.12+, FastAPI, uvicorn** (ASGI).
- **SQLite (stdlib `sqlite3`) with FTS5**, WAL mode. No ORM.
- **Static SPA** — vanilla HTML/CSS/JS, no build step, served via
  `StaticFiles` from `src/ebooksearch/static/`.
- **`uv` + `pyproject.toml`**, src layout (package `ebooksearch/`).
- Deps: `fastapi`, `uvicorn[standard]`, `ebooklib` (EPUB), `pypdf` (PDF),
  `watchdog`. Dev deps: `pytest`, `httpx`.

## Module map

```
src/ebooksearch/
  __init__.py        — version
  config.py          — env-driven Config dataclass
  db.py              — schema, connect(), init_db()
  models.py          — ParsedBook, ParseError, BookRow
  extractors.py      — registry of {extension → extractor}, EPUB + PDF,
                       lazy preview helpers, file-discovery walk
  progress.py        — thread-safe ProgressState snapshot for SSE
  indexer.py         — IndexManager: parse pool + writer thread + dispatcher
  watcher.py         — debounced watchdog → IndexManager
  search.py          — FTS query builder + read-only query helpers
  main.py            — FastAPI app, routes, SSE bridge, lifespan
  static/            — index.html, styles.css, app.js
```

## Core architectural decisions (do not change without understanding)

1. **Parse in parallel, write serially.** `ThreadPoolExecutor` parses files
   concurrently; one dedicated writer thread owns the single SQLite write
   connection and drains a queue in batched transactions. Parse jobs **must
   never touch the DB**. This respects SQLite's single-writer model while
   parallelizing the real bottleneck (parsing). See `indexer.py:_Writer`.

2. **WAL for concurrent reads.** Search/download/recent endpoints run on
   short-lived read-only connections while indexing is in progress. Verified
   working — don't switch journal mode.

3. **External-content FTS5** (`books_fts`). FTS table holds no duplicate
   text; INSERT/UPDATE/DELETE triggers keep it in sync. Tokenizer:
   `unicode61 remove_diacritics=2`. BM25 weights title > author > filename
   (3.0 / 2.0 / 1.0).

4. **Search auto-prefixes every token** (`search.build_match_query`). So
   `mob` matches `Moby-Dick`. Users typing `*` explicitly still works
   (trailing `*` is preserved, not doubled).

5. **Single-flight + coalesced follow-up.** Only one indexing run at a time
   (`_run_lock`). Subsequent requests get unioned into one `_queued_followup`.
   If the queued set exceeds `_TARGETED_PROMOTE_THRESHOLD` (1000), it's
   promoted to a full scan — cheaper than chasing thousands of individual
   paths and bounds memory.

6. **Targeted runs check mtime/size before parsing.** Critical: without this,
   spurious watchdog events on NAS filesystems cause perpetual re-scans.
   `_run_targeted` queries the DB for fingerprints of just the affected paths
   and skips unchanged ones. Don't remove.

7. **Move detection via `content_hash`.** Upserts that find an existing row
   with the same hash whose old path is gone from disk rewrite that row's
   path in place (preserves `id` and `indexed_at`). `submit_upsert` classifies
   added vs updated vs moved via short indexed SQL reads — no in-memory
   path/hash cache (memory used to scale with library size; doesn't anymore).

8. **SSE for live progress.** One-way; bridges sync threads → asyncio via
   `loop.call_soon_threadsafe`. Terminal events carry the finalized
   `index_runs` row so the UI updates "last reindex" without polling. Don't
   convert to WebSockets without a good reason.

9. **Two-tier size limits.**
   - **`MAX_FILE_BYTES`** (default **50 MiB**) is the raw open cap, checked
     at scan time. Anything larger never reaches the parse pool — `pypdf` /
     `ebooklib` load whole files into RAM, and with N parse-pool workers a
     single huge book can balloon to N× its uncompressed size.
   - **`MAX_TEXT_BYTES`** (default 5 MiB) caps the *extractable text* inside
     a file. For EPUBs this is measured by peeking the zip central directory
     (no decompression) and summing `.html`/`.xhtml`/`.htm` uncompressed
     sizes — safe under parallelism, negligible memory cost. PDFs have no
     cheap pre-parse measurement and are only bounded by `MAX_FILE_BYTES`.
   - Files that fail either cap are recorded in the error list with a clear
     "exceeds limit" / "exceeds text limit" message and aren't indexed.

10. **Targeted upserts run before deletes** (`_run_targeted`). A rename
    pairs a created+deleted event; if delete fires first, move detection
    can't find the old row.

## Configuration (env vars)

| Var | Default | Notes |
| -- | -- | -- |
| `EBOOK_DIR` | `./test-ebooks` | Root folder to index (recursive). |
| `DB_PATH` | `./index.db` | SQLite database path. |
| `REINDEX_ON_STARTUP` | `true` | Run a full scan on boot. |
| `INDEX_WORKERS` | `min(8, cpu_count)` | Parse-pool size. |
| `WATCH_DEBOUNCE_SECONDS` | `2.5` | Debounce window for folder events. |
| `WRITE_BATCH` | `100` | Rows per write transaction. |
| `MAX_FILE_BYTES` | `52428800` | Raw open cap (50 MiB). Files larger never reach the parse pool. |
| `MAX_TEXT_BYTES` | `5242880` | Extractable-text cap (5 MiB). EPUBs whose HTML/XHTML body exceeds this are skipped. |
| `LOG_LEVEL` | `INFO` | DEBUG for verbose troubleshooting. |

## Common commands

```bash
# install
uv sync --extra dev

# fixtures
uv run python scripts/build_fixtures.py

# run
EBOOK_DIR=./test-ebooks uv run uvicorn ebooksearch.main:app --reload

# tests
uv run pytest

# docker (local)
make build-local && make run

# docker (multi-arch + push)
make build           # → ghcr.io/baudcode/ebooksearch:vX.Y.Z + :latest
make local           # same image, pushed to tower.local:5000 (LAN registry)
make version         # prints current version from pyproject.toml
```

The Makefile reads the version from `pyproject.toml`. Bump that file to cut
a release; `make build` will tag accordingly.

## Releasing

CI/CD lives in `.github/workflows/`:

- `ci.yml` — runs `pytest` on every push to `main` and on every PR.
- `docker.yml` — on every push to `main`, builds a multi-arch image and
  pushes to `ghcr.io/baudcode/ebooksearch`. The image is tagged with
  `:X.Y.Z`, `:vX.Y.Z`, `:latest`, `:main`, and `:sha-<short>`. Uses
  `GITHUB_TOKEN` for auth (no PAT needed). Cached via the GitHub Actions
  cache backend.

**Versioning is single-sourced from `pyproject.toml`.** Both the Makefile and
the docker workflow read it. The `ebooksearch.__version__` Python attribute
is resolved at import time from package metadata via `importlib.metadata`, so
there's no duplicate string to maintain.

To cut a release:
1. Bump `version = "X.Y.Z"` in `pyproject.toml`.
2. Commit, push to `main`. That's it — no git tag required. The workflow
   tags the image accordingly.

## Code style / conventions

- **No comments that restate what the code does.** Comment only the *why* —
  hidden constraints, invariants, workarounds. Most modules have a short
  docstring at the top explaining their role; that's enough.
- **Type hints throughout.** `from __future__ import annotations` at the top
  of every module.
- **`run_in_threadpool` for blocking file/SQLite work on the request path.**
- **Per-file error isolation in the indexer.** One bad file never aborts a
  run — caught by `_parse_and_write`'s `try/except`, recorded in
  `progress.errors` (capped at 50).
- **Trigger label** in `index_runs.trigger` is one of: `startup`, `manual`,
  `watch`. Don't add new ones without updating the UI's `triggerLabel`.
- **UTC timestamps** in the DB, ISO 8601 with seconds precision. UI renders
  relative time + absolute on hover.
- **Static UI** has no build step; edit `static/*` directly. The dev server's
  `--reload` doesn't watch static files — refresh the browser.

## Testing

Tests live in `tests/`, run via `uv run pytest`. The conftest builds tiny
synthetic EPUB/PDF files programmatically (no external fixtures committed).

When adding indexer behavior:
- Use the `ebook_env` fixture for an isolated `ebook_dir` + `db_path`.
- Use `_wait_idle(manager)` from `test_indexer.py` to block on a run finishing.
- Don't sleep — wait on the SSE-style terminal callback.

## Diagnostic tips

- **"Why is it re-scanning?"** Set `LOG_LEVEL=DEBUG` (or just leave at INFO,
  which already shows every watchdog event + every run start/done). Look for
  `watchdog event: type=…` lines firing repeatedly while idle — that's the FS
  layer being noisy. Common causes: SMB/NFS shares, backup tools touching
  mtime, browser/editor temp files.
- **"Search isn't matching."** Remember tokens are auto-prefixed. `mob` →
  `"mob"*` — but only `"-Dick"` is its own token in `Moby-Dick` because
  `unicode61` splits on punctuation. Lowercase comparison is automatic.
- **Container dies randomly.** Check `docker inspect <name> --format
  '{{.State.OOMKilled}}'`. If true, raise memory or lower `INDEX_WORKERS` /
  `MAX_FILE_BYTES` / `MAX_TEXT_BYTES`.

## Things deliberately not built

These are intentional non-goals — don't add them without a discussion:

- Full-text-of-contents search. The FTS table has a clean extension point
  (add a `content` column + plumb it through `_upsert`). Default scope is
  metadata only.
- Semantic / vector search. Could add `sqlite-vec` + an ANN query path with
  a UI mode toggle.
- MOBI/AZW3 support. Trivial to add via `extractors._REGISTRY` once you
  pick a parser (`mobi` package or Calibre's `ebook-convert`).
- Auth. Designed to live behind a reverse proxy / VPN on a home network.
- Async DB. stdlib `sqlite3` + threadpool is fine for this workload.

## When in doubt

Read the original spec in `spec.md` — it documents the design and the
tradeoffs in detail.
