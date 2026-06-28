# ebooksearch

[![CI](https://github.com/baudcode/ebooksearch/actions/workflows/ci.yml/badge.svg)](https://github.com/baudcode/ebooksearch/actions/workflows/ci.yml)
[![Docker](https://github.com/baudcode/ebooksearch/actions/workflows/docker.yml/badge.svg)](https://github.com/baudcode/ebooksearch/actions/workflows/docker.yml)

A local ebook search server. Point it at a folder of `.epub` / `.pdf` files;
it indexes metadata into SQLite FTS5 and serves a single-page web UI for
search, browse, preview, and download. The index auto-updates when the folder
changes.

Designed to run as a single container on a NAS, pointed at a mounted library.

## Features

- **Fast metadata search** over title, author, and filename — FTS5 with BM25
  ranking, auto-prefix on every token (so `mob` matches `Moby-Dick`).
- **Lazy content preview** — snippet + table of contents extracted on demand,
  cached by content hash.
- **Live indexing UI** — progress bar, throughput, ETA, current file, error
  list, all streamed via Server-Sent Events.
- **Auto-reindex on folder changes** — debounced watchdog routes targeted
  updates through the same single-flight pipeline as manual reindex.
- **Rename detection** — moves preserve the database `id` rather than
  churning a new row.
- **File size limit** — oversize PDFs/EPUBs are skipped at scan time to keep
  memory bounded (default 5 MiB, configurable).
- **Concurrent reads during indexing** — WAL mode lets search/download
  endpoints serve traffic while a reindex runs.

## Quick start

```bash
# install
uv sync --extra dev

# generate a few synthetic fixtures (optional)
uv run python scripts/build_fixtures.py

# run
EBOOK_DIR=./test-ebooks uv run uvicorn ebooksearch.main:app --reload
```

Open <http://127.0.0.1:8000>.

## Docker

Prebuilt multi-arch images (`linux/amd64` + `linux/arm64`) are published to
GitHub Container Registry on every push to `main` and every tag:

```bash
docker run -d \
  --name ebooksearch \
  -p 8000:8000 \
  -v /path/to/your/library:/data/books \
  -v /path/to/persistent-state:/data \
  --memory=1g --memory-swap=1g \
  -e MAX_FILE_BYTES=52428800 \
  -e MAX_TEXT_BYTES=5242880 \
  ghcr.io/baudcode/ebooksearch:latest
```

Or pin to a specific version, e.g. `ghcr.io/baudcode/ebooksearch:v0.2.1`.

```bash
# build + run locally (single-arch, loads into local docker daemon)
make build-local
make run
```

`/data` is the volume for the SQLite database. `/data/books` is where your
library mounts.

### Building yourself

Tags follow the version in `pyproject.toml` (`make version` prints it).

```bash
# multi-arch build + push to ghcr.io (the default)
make build

# push to a private LAN registry instead (defaults to tower.local:5000)
make local

# or override registry explicitly
make build REGISTRY=registry.example.com/your-org TAG=v0.2.1
```

The `Makefile` uses `docker buildx` with both `linux/amd64` and `linux/arm64`
platforms by default. For plain-HTTP registries on a LAN, edit
`buildkitd.toml` to add yours.

## Configuration

All config is via environment variables:

| Variable | Default | Notes |
| -- | -- | -- |
| `EBOOK_DIR` | `./test-ebooks` | Root folder to index (recursive). |
| `DB_PATH` | `./index.db` | SQLite database path. |
| `REINDEX_ON_STARTUP` | `true` | Run a full scan on boot. |
| `INDEX_WORKERS` | `min(8, cpu_count)` | Parse-pool size. |
| `WATCH_DEBOUNCE_SECONDS` | `2.5` | Debounce window for folder events. |
| `WRITE_BATCH` | `100` | Rows per write transaction. |
| `MAX_FILE_BYTES` | `52428800` (50 MiB) | Raw open cap. Files larger than this never reach the parse pool — protects RAM under N parallel workers. |
| `MAX_TEXT_BYTES` | `5242880` (5 MiB) | Extractable-text cap. EPUBs whose HTML/XHTML body exceeds this are skipped (measured from the zip directory without decompressing). |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose troubleshooting. |

## HTTP API

| Method | Path | Returns |
| -- | -- | -- |
| `GET` | `/api/search?q=&limit=&offset=` | Ranked compact result rows. |
| `GET` | `/api/recent?limit=` | Most-recently indexed books. |
| `GET` | `/api/book/{id}` | Full metadata + snippet + TOC. |
| `GET` | `/api/download/{id}` | Streams the file. |
| `GET` | `/api/stats` | `{total_books, db_size_bytes, last_run}`. |
| `GET` | `/api/index/status` | Current `ProgressState` snapshot. |
| `GET` | `/api/index/runs?limit=` | Recent `index_runs` rows. |
| `GET` | `/api/index/stream` | Server-Sent Events: live progress. |
| `POST` | `/api/reindex` | `202`; full scan kicked off in the background. |

## Architecture in one paragraph

A `ThreadPoolExecutor` parses files in parallel — pure read-only work. One
dedicated writer thread owns the single SQLite write connection and drains a
queue of parse results in batched transactions. Only one indexing run is
active at a time, guarded by a lock; additional requests coalesce into one
queued follow-up. WAL mode lets the read endpoints serve traffic during
indexing. Live progress is streamed to the UI over SSE; sync writer threads
hand events to the asyncio loop via `call_soon_threadsafe`.

More detail in [`CLAUDE.md`](./CLAUDE.md) and [`spec.md`](./spec.md).

## Development

```bash
# run tests
uv run pytest

# server with debug logging
LOG_LEVEL=DEBUG uv run uvicorn ebooksearch.main:app --reload
```

Tests build synthetic EPUB/PDF files programmatically — no external fixtures
in the repo.

## License

MIT
