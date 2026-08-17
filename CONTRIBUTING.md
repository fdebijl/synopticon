# Synopticon Contribution Guide
This guide lays out how to contribute to Synopticon. Contributions made using LLM's are welcome, as are fully human-coded contributions. Features maybe merged at the discretion of the maintainers.

## Requirements
- Python v3.11-3.12
- Node v22+

## Development

```bash
uv sync --extra cpu --extra review --extra faiss   # or --extra gpu instead of cpu
uv run pytest            # unit tests; fully mocked, never touch a NAS
cd frontend && npm ci && npm run build   # GUI only: build the Vue SPA (Node 22+); npm run dev for hot-reload

# PostgreSQL backend tests; skipped unless a throwaway server is pointed at
SYNOPTICON_TEST_POSTGRES_DSN=postgresql://user@127.0.0.1:5432/synopticon_test \
    uv run pytest tests/unit/test_postgres_backend.py -q
```

(`--all-extras` no longer works: the `cpu`/`gpu` extras are mutually exclusive, so pick one explicitly.) The Python test suite needs no Node — the frontend build (typechecked by `vue-tsc`) runs as its own CI job. See [Web GUI](#web-gui) for the two-server dev loop.

Layout: `syno/` (API client + write-back) · `sync/` (extraction/caching + content hashing) · `pipeline/` (detect/align/embed) · `cluster/` (graph, Chinese Whispers, cross-reference) · `dedupe.py` (hash-based duplicate detection) · `eval/` (hold-out tuning) · `review/` (report + UI). The `cluster/` and `dedupe` layers deliberately import nothing from `syno/`/`pipeline/` — clustering and duplicate *detection* can never touch the network; only the write-back halves do.

For working on the UI, run the two dev servers side by side: `uv run synopticon web` (backend on :8686) and, in another terminal, `cd frontend && npm run dev` (Vite on :5173, hot-reload). Vite proxies `/api` and `/crops` to the backend, so open the app at `http://127.0.0.1:5173` and the session cookie works same-origin.

## Patterns

### Structured progress events

Every command can emit machine-readable progress. Set `SYNOPTICON_PROGRESS_FILE=<path>` and each run appends newline-delimited JSON events to that file (unset → no-op; terminal output is byte-identical either way). This is how the GUI's job runner tracks live progress, and it's equally usable from your own tooling. The v1 schema is one JSON object per line, consumers ignoring unknown fields:

```jsonl
{"v":1,"ts":1699999999.1,"event":"phase","phase":"extract"}
{"v":1,"ts":1699999999.2,"event":"progress","phase":"extract","done":842,"total":12290}
{"v":1,"ts":1699999999.3,"event":"log","level":"warning","message":"skipped photo 123"}
{"v":1,"ts":1699999999.4,"event":"result","ok":true,"stats":{"photos_processed":412}}
```

The process **exit code is authoritative** for success/failure; the `result`/`error` events are advisory niceties.

## Gotcha's

### Jobs and the GUI's responsiveness

A job launched from the GUI is a subprocess sharing the machine with the single-process web server, so it is deliberately constrained: it gets `nproc - 1` BLAS/OpenMP threads (`OMP_NUM_THREADS` and friends) and runs at niceness 10. Both are configurable via `inference.job_threads` and `inference.job_nice`; a thread variable you export yourself always wins, and `job_threads = 0` leaves the environment untouched.

Without this, the numeric stacks size their pools to the whole machine and busy-spin between calls — a clustering run puts a hot thread on every core, and the web server then needs tens of seconds of wall-clock to do a millisecond of work. The symptom is distinctive and worth recognising: **unrelated requests all taking 30–90 s and completing at the same instant**, while the server itself looks healthy. If you see that, check the `slow request:` / `event loop stalled` lines in the server log — each carries a CPU/IO pressure snapshot (`cpu stall 84%, io stall 3%, load 15.9/16`) that distinguishes "this box is oversubscribed" from a problem inside the app.
