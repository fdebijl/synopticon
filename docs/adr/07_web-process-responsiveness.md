# ADR 07 — Web process responsiveness

**Status:** Accepted — do not regress
**Applies to:** `web/app.py`, `web/stats.py`, `web/ops_routes.py`, `review/lookups.py`, `review/queries.py`

## Context

The GUI is **one uvicorn process**. A single blocking call therefore stalls *every* concurrent
request, not just its own. The tell is a batch of unrelated requests all completing at the same
instant.

Every stall mode looks identical from the client side — the browser sees slow requests and nothing
else. That symmetry is why this document exists and why the server carries a watchdog that names
which mode it was.

## Decision

Four hard invariants, plus instrumentation that distinguishes the failure modes, plus caches at
the three places measurement showed were O(library) per request.

---

## The four invariants

### (1) No blocking I/O on the event loop

An `async def` handler must do its SQLite, filesystem, network or scrypt work inside a
`run_in_threadpool` closure. A sync `def` handler is fine — Starlette threadpools it for you — but
the moment a route needs `await request.json()` it becomes `async`, and everything after that
is on the loop.

Currently threadpooled for this reason:

- the auth middleware's `_auth_lookup`
- both scrypt paths (`/api/auth/login`, `/api/auth/create-account`, and `configio`'s
  change-password) — scrypt is ~100 ms of deliberate CPU *per call*
- every review mutation (decide / bulk / name)
- `jm.submit`
- `write_config`
- `setup_routes`' NAS `probe`, a blocking HTTPS round-trip that would otherwise freeze the GUI for
  the whole timeout

Supporting short-circuits: `first_boot` is a one-way latch (`have_users`), since no route deletes
users; `/api/*` and `/crops/*` paths skip the `_resolve_dist_file` stat entirely; and a request
presenting *no* credential at all short-circuits to `(None, False)` without opening a connection
once `have_users` is latched. That last one covers the entire loopback-healthcheck path and every
401 — both of which used to open a SQLite connection just to be told they were anonymous.

### (2) SSE handlers must be async generators

A sync generator is iterated via Starlette's `iterate_in_threadpool`, so each open stream occupies
an AnyIO worker thread between yields — up to the 15 s ping — out of a pool shared with every sync
route handler. 40 streams measurably starved the whole API for ~10 s.

`/api/jobs/{id}/stream` uses `await asyncio.sleep` plus an `await request.is_disconnected()` check,
so an abandoned client cannot leave the loop spinning.

### (3) The web process must never import `pipeline.runner`

Nor anything else that pulls `cv2` or `onnxruntime` at module scope.

`pipeline_version` therefore lives in the leaf module `pipeline/version.py` (hashlib +
`manifest_bytes` only; `runner` re-exports it for the CLI's sake). `pipeline/crops.py` defers its
`align`/`runner` imports into `regen_crops`/`_crops_present`, so `ops_routes` can call
`crops_disk_usage` — a plain directory walk — without the image stack.

This was a real 20+ second stall: the first `/api/stats` after a restart paged `cv2` in *inside a
request handler*. Because the request that triggered it was an aborted dashboard poll, the culprit
never appeared in the client's own HAR — only three unrelated requests all finishing at the same
instant did.

`tests/unit/test_web_responsiveness.py` locks the invariant in via a subprocess `sys.modules`
check. It has to be a subprocess: the pytest session imports `cv2` for the pipeline tests, which
would mask the violation in-process.

### (4) Every middleware must be pure ASGI, never `@app.middleware("http")`

Starlette's `BaseHTTPMiddleware` relays the response through a memory object stream inside a task
group. With the three layers here, every SSE event crossed nine hops on the loop. Worse, a client
that disconnects mid-stream leaves the inner app finishing without a response start, which
`BaseHTTPMiddleware` turns into a spurious, un-catchable `RuntimeError: No response returned.` —
logged on every abandoned job stream and every shutdown with one open.

`_AuthMiddleware`, `_NoStoreAPI` and `_RequestTiming` are therefore plain
`__call__(scope, receive, send)` classes registered with `add_middleware`. **Last added is
outermost**, so the order of those three calls is load-bearing. Header defaulting is done by
wrapping `send` (`_defaulting_send`), and the auth verdict travels in `scope["state"]`, which is
the same dict the handler's `request.state` wraps.

`test_middleware_stack_is_pure_asgi` locks it in.

---

## Liveness probe — `GET /api/health`

The one endpoint a container healthcheck may point at.

`_AuthMiddleware` short-circuits it *first*, before the `/assets` bypass and before the
`run_in_threadpool(_auth_lookup, …)` hop, and the handler is `async def` returning a literal. So it
takes no AnyIO worker thread, opens no SQLite connection, and stats no file. Keep it that way.

The failure it exists to prevent: a probe pointed at `/api/auth/me` or `/api/stats` times out
during a long `extract` — the job's crop writes and per-photo commits saturate the same, often
NFS-backed, `/data` the server reads from, every database-touching handler parks in D-state, and
the 40-slot threadpool fills — so the orchestrator restarts the container out from under a
multi-hour job.

Related: the anonymous short-circuit (`have_users and _anonymous(request)`) is taken **on the
event loop** in the middleware rather than inside `_auth_lookup`'s threadpool call, so every 401
and every cookieless poll is likewise thread-free.

Two tests in `test_web_responsiveness.py` lock in the zero-connection and coroutine properties.

The image ships **no `HEALTHCHECK`** — the same entrypoint serves one-shot commands, which a
built-in probe would mark unhealthy. README's "Healthchecks" section carries the service-level
recipe instead, and must stay in sync with this endpoint.

---

## Responsiveness watchdog

Because all the stall modes look identical from the client side, the server names which one it
was. A lifespan task ticks every `_WATCHDOG_TICK` (250 ms) and logs to the `synopticon.web` logger,
wired to uvicorn's stderr handler in `serve`:

| Signal | Threshold | Line |
|---|---|---|
| loop lag | ≥ `_WATCHDOG_STALL` (1 s) | `event loop stalled for Xs` |
| AnyIO pool exhaustion | `borrowed >= total`, one line per 10 s | `AnyIO worker pool saturated` |
| slow handler | `_SLOW_REQUEST` (3 s), throttled per `METHOD /path` | logged by the outermost `_RequestTiming` |

Slow-request timing stops at `http.response.start`, not at the end of the body — otherwise every
job SSE stream logs as a multi-minute "slow request". The per-path throttle exists so one
incident's released queue does not bury the first line.

Pool saturation matters as much as loop lag: the auth middleware needs a worker thread for *every*
request, so a full pool queues even a static `index.html` while the loop looks perfectly healthy.

**Every line carries two extra things:**

- The in-flight request set with per-request ages, capped at `_IN_FLIGHT_REPORT_MAX` then `+N more`.
- `_pressure_report()` — Linux PSI `some avg10` for cpu and io, plus the 1-minute load average,
  read lazily and only when something is already being logged. That fourth signal is what the
  three internal ones structurally cannot express: *the process was runnable and the kernel
  scheduled a job subprocess instead*. From inside, that looks like perfect health — no lag, idle
  threadpool — while requests take 90 s. See ADR 06 for the countermeasure.

**The stall line reports `during stall:`** using `_in_flight_report(now, since=before)`, which
folds in `recently_done` (a 32-entry ring, tagged `done`). Whatever blocked the loop has
necessarily *finished* before the watchdog can run again, so reporting only what is in flight
"now" names everything except the culprit. This used to work by accident —
`BaseHTTPMiddleware`'s memory-stream await happened to yield to the loop before the in-flight
entry was removed — and stopped the moment the middleware became pure ASGI under invariant (4).

---

## Caches, and why each exists

### Review lookups must stay O(page) (`review/lookups.py`)

`load_review_items` needs three maps derived from the *whole* library: `face_crops`,
`hidden_persons`, and `person_faces` (which runs `crossref.label_faces`' full IoU ground-truth
match).

Rebuilding them per request cost **2.7 s per 100-item page** on a 56k-face library, re-paid on
every infinite-scroll fetch. Four concurrent pages saturated the threadpool and hung the whole API.

`LookupCache` caches all three, keyed on a ~6 ms aggregate `fingerprint()` over `photos`, `faces`,
`syno_faces`, `person_photos` and `persons` — counts, max ids, and sums for the columns that change
in place (`persons.show`, `syno_faces.person_id`).

**The key must never include `review_queue`.** A job mutating faces has to invalidate, but
approving an item must not, or every keystroke triggers a full rebuild.

Net: 2752 ms → ~8 ms warm, byte-identical output.

Related: `queries.crop_url_mapper` resolves the crops root once instead of a `realpath` syscall per
face (3.0 s → 0.12 s for 56k), and `_link_map` batches the similar-group deep-link resolution into
one query per space.

### Auth caching

The middleware caches validated **session cookies** for `_AUTH_CACHE_TTL` (30 s), so one page
load's burst of crop requests does not open a SQLite connection per image. 30 s is deliberately
longer than every SPA polling interval (5 s jobs, 15 s counts); at an earlier 2 s it never once hit
for the steady-state traffic it was meant to collapse.

API keys are deliberately *not* cached — `_credential` returns `None` for a Bearer header. A
browser never sends one, so there is nothing to collapse, and key revocation stays exact.

Logout and password change call `_invalidate_auth_cache` (exposed as
`app.state.invalidate_auth_cache`), so in-process revocation is immediate. The TTL window only ever
applies to a session killed by another process, i.e. `synopticon reset-password`.

### Cheaper repeated work

- `store.connect` skips its `commit()` when `user_version` is already current — it runs per request.
- `web/stats.py` memoizes `pipeline_version` on the manifest's `(mtime_ns, size)`, to skip
  re-reading the manifest per poll. `/api/about` reads that same
  `stats._pipeline_version_cached`, which is how it stays inside invariant (3).
- `ops_routes` caches the crops disk walk for 60 s. `crops_disk_usage` itself walks with
  `os.scandir`, not `Path.rglob` — one stat per entry instead of two, 0.96 s → 0.24 s over 112k
  crops.

### Browser caching and compression

| Path | `Cache-Control` | Why |
|---|---|---|
| `/assets` | `public, max-age=31536000, immutable` | content-hashed by Vite |
| `/crops` | `public, max-age=86400` | a day, not a year — `regen-crops` can rewrite one in place |
| dist-root files (favicons, manifest) | one hour | |
| `index.html` | `no-cache` | it names the hashed bundle; caching it pins an open tab to a stale deploy |
| `/api/*` | `no-store` | |

The `/api/*` `no-store` comes from a middleware registered *after* (i.e. wrapping) the auth
middleware, so short-circuited 401/302 replies are covered too. It only *defaults* the header,
which is how `/api/quickmerger/thumb` can override it with `private, max-age=86400`.

`Cache-Control` is stamped in `_CachedStatic.file_response`, which survives into Starlette's 304.

Gzip is on via `_add_gzip` — a 100-item review page goes 97 KiB → 11 KiB — but **path-guarded to
skip `/crops`**: those are already-compressed JPEG/PNG and the responder runs on the event loop.
Starlette's `GZipMiddleware` already excludes `text/event-stream`, so the job SSE stream still
flushes per event.

## Consequences

- Any new route that touches the database, the filesystem, or the network needs an explicit answer
  to "which thread does this run on?"
- Any new middleware is pure ASGI, and its registration order matters.
- Any new per-request derived map over the whole library needs a fingerprint-keyed cache, not a
  recomputation.
