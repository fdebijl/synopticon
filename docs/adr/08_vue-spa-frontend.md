# ADR 08 — Vue SPA frontend behind a JSON-only backend

**Status:** Accepted
**Applies to:** `frontend/`, `src/synopticon/web/dist/`, `web/app.py`, `web/configio.py`, `web/quickmerger.py`

## Context

The GUI began as server-rendered Jinja templates with hand-written JavaScript. Cards were defined
twice — once as a Jinja macro, once in a `renderCard()` function — and the two drifted. State was
smuggled into pages via `window.SYN_*` embeds.

Meanwhile the backend needed to stay importable without the `[review]` extra so the CLI stays
fast, and the web process needed a hard ceiling on what it imports (ADR 07).

## Decision

A Vue 3 + TypeScript SPA built by Vite, served by a FastAPI backend that speaks only JSON.

### Layout and build

`frontend/` at the repo root: `src/{views,components,composables,stores,api}` plus
`styles/app.css`. Runtime dependencies are just `vue` and `vue-router` — shared state is
module-scoped reactive singletons in `stores/`, no Pinia.

`styles/app.css` carries only what is genuinely shared: the design tokens, the reset, the app
shell (sidebar/topbar/page) and the primitives several views use (`.card`, `.btn`, `.badge`,
`.grid`, `.skeleton`, toasts, tables). Everything belonging to one view or component lives in that
SFC's `<style scoped>` block, so the styles ship with the route chunk and cannot leak. The one
exception is `styles/auth.css`, imported by both `LoginView` and `SetupView`, which style form rows
nested arbitrarily deep inside the shared card shell.

`npm run build` runs `vue-tsc -b` (strict typecheck) then `vite build`, emitting into
`src/synopticon/web/dist/` (Vite's `outDir` is `../src/synopticon/web/dist` relative to
`frontend/`).

**`dist/` is gitignored and never committed.** Consequences that are easy to trip over:

- Docker builds it in a Node stage; CI builds it in a `frontend` job.
- Wheels need `artifacts = ["src/synopticon/web/dist"]` in
  `[tool.hatch.build.targets.wheel]`, because hatchling excludes VCS-ignored files by default.
  Without it the SPA silently drops out of the wheel.
- `synopticon web` pre-flights `_check_dist_built`: a missing `dist/index.html` exits with build
  instructions rather than serving a broken page.

Dev loop: `uv run synopticon web` (:8686) plus `cd frontend && npm run dev` (:5173, which proxies
`/api` and `/crops` to the backend so the SameSite=Lax cookie stays same-origin). **The SPA must
only ever use relative URLs.**

### SPA serving

`_DIST_DIR = Path(__file__).parent / "dist"`. `create_app`'s `dist_dir` seam — mirroring the
`job_manager` seam — lets tests inject a temporary dist, so pytest never needs a real build.

`/assets` is mounted publicly, and only if it exists: it is a content-hashed bundle with no user
data, which replaced an older `/static` auth bypass.

A catch-all `GET /{path:path}` is registered **last**:

- unknown `api/*` → 404 JSON, never HTML
- a real dist-root file (favicon, manifest, img — traversal-checked via `_resolve_dist_file`) →
  served directly
- otherwise → `index.html`
- **dist absent → 503 `{"error": "frontend not built", "hint": ...}`**, so `create_app` and API
  tests work with no Node installed

### The backend is JSON-only

No Jinja templates or hand-written JS remain in `web/`. `jinja2` stays in the *core* dependencies,
but only the static-HTML `report` command uses it now.

FastAPI, uvicorn and tomlkit sit behind the `[review]` extra, guarded by `_require_fastapi()`, so
everything imports cleanly without it.

Three route modules must **not** carry `from __future__ import annotations`: `quickmerger.py`,
`schedule_routes.py` and `backup_routes.py`. FastAPI resolves handler annotations against module
globals, and `Request` is imported inside the registrar in all three. With postponed evaluation,
every route silently degrades its `Request` parameter to a required query field — 422 on every
call.

## Frontend conventions that are easy to regress

### One poller, chained, visibility-gated

`stores/jobs.ts` is the **single** `/api/jobs` poller. Views subscribe via `startJobPolling`,
`state.running` and `state.history` rather than adding their own timers — the topbar, Dashboard and
Pipeline each ran one for identical data, tripling the request rate.

It is `setTimeout`-chained with an in-flight guard. A bare `setInterval` keeps firing while earlier
requests hang, so backend slowness accumulates unbounded concurrent requests and a latency blip
becomes an unrecoverable pile-up. Plus error backoff (5 s → 60 s), a slower idle cadence (15 s
versus 5 s while a job runs), and `visibilitychange` gating so background tabs stop polling.

`SidebarNav`'s review-counts poll follows the same pattern. **Any new poller must keep these
properties.**

`useJobStream`'s `attach(id)` is **idempotent** — a no-op when already following that live job —
and `connectSSE` closes any existing `EventSource` before opening one. `start()` attaches on the
POST response, and a route change to `/jobs/<id>` mounts a panel that attaches again; without both
guards that left two server-side SSE generators per job, with the whole event ring replayed into
the second.

### One job panel

Every view that runs a job — Pipeline, Apply, Maintenance, Setup (×2), `/jobs/:id` — mounts the
*same* `components/JobPanel.vue`, so improvements land everywhere at once. Don't build a second one.

`composables/useJobStream.ts` owns the transport plus the derived display state:

- throughput over a trailing 30 s window, not since phase start, so rate and ETA follow a run that
  speeds up or slows down
- elapsed from the job's own `started_at`/`ended_at`, so attaching to a job already in flight
  reports its real runtime rather than time-since-mount
- phase transitions narrated into the log as `▶ <phase>` lines
- a closing `✓ completed` / `failed (exit code N)` line, so the log always says how the run ended

`JobPanel` renders `phase (space) · done / total (pct) · rate · ETA` above the bar. The bar alone
made a running job look identical to a stalled one. The topbar chip in `App.vue` uses the listing's
`progress` snapshot for pages with no panel.

### One card definition

`components/review/ReviewCard.vue` is the single source of truth for a review card. Grid and Focus
are two components (`ReviewGrid.vue` / `ReviewFocus.vue`, switched by `v-if` on the `view` query)
rendering the *same* reactive list — no dual-DOM projection. The choice persists in the route
(`?view=focus`) and in `localStorage`.

`ReviewView.vue` owns the reactive `items[]` and selected index, with kind/status/view in the route
query; infinite scroll fetches `/api/review/items`, and there is an undo stack plus a keyboard flow
(y/n/s/j/k/u/arrows via `useKeyboard`).

Every crop `<img>` in `ReviewCard.vue` and `ReviewFocus.vue` carries `loading="lazy"
decoding="async"`. A 100-item grid otherwise fires 100+ image requests at once on every scroll
page. The fixed `width`/`height` in those components' scoped styles mean lazy loading costs no
layout shift.

### Server-driven forms

The Schedules editor is rendered entirely from the server's catalog — `GET /api/schedules` returns
`{items, jobs}`, and field keys, types, options and defaults all come from `schedules.SCHEDULABLE`.
Adding a schedulable parameter is a Python-side change only. The cron box previews its next five
firings through `POST /api/schedules/preview` (debounced 300 ms), which is also how a bad
expression gets its error message.

`SettingsView.vue` is likewise schema-driven: a `SchemaForm` over `Settings.model_json_schema()`.
Tab order and labels live in `utils/schema.ts` (`SECTIONS` / `LABELS`); `AccessTab.vue` (password,
API keys) and `ModelsTab.vue` (read-only weights status from `GET /api/models`) are appended in
`TABS` as pseudo-tabs rather than being driven by the schema.

**Field help text is two-tier.** `config.py`'s `description=` is the plain-language half — what the
setting does, then practical guidance — and renders inline under the control.
`json_schema_extra={"details": ...}` is the technical half — units, internals, cache invalidation,
diagnostics — and renders in `SchemaField.vue`'s collapsed, dimmed `<details>` block. Both are
`white-space: pre-wrap`, so `\n\n` is a real paragraph break. Keep new fields to that shape rather
than opening a description with jargon.

### Utilities versus Maintenance

`/utilities` holds tools that act on the **NAS library**. Maintenance keeps the ones that only drop
**local** pipeline state. Photo dedupe lives in the former for that reason — same job, same
typed-phrase gate, only the host view differs.

## QuickMerger backend notes

QuickMerger is a port of `har/quickmerger.js`: it walks the unnamed people of a space and, per
person, sets a name, merges into a suggested person, hides, or skips. Its write gates are in
ADR 05. Backend details that are easy to regress:

- **All NAS access goes through one lazily-built, reused `SynoClient`** (`NasSession`). A client
  per request re-logins on every keystroke of the suggest box. Every use is serialized under one
  lock, and its SQLite connection is opened `check_same_thread=False` because threadpool workers
  differ per request. `create_app`'s lifespan closes it via `app.state.nas_session`.
- The person listing passes `show_more=True` (Synology's "show more people"). Without it, the
  low-item-count tail — the entire backlog the tool exists for — is absent. That is why
  `foto.list_persons` takes the flag while `sync` deliberately leaves it off.
- Thumbnails are **buffered, not streamed** (`/api/quickmerger/thumb`), since a `StreamingResponse`
  would hold the session lock for the whole body.
- The unnamed-person worklist is cached per space for 5 minutes (`refresh=true` bypasses), and each
  successful write drops its person from the cache.

## Consequences

- Adding a config section means adding it to `utils/schema.ts`'s `SECTIONS`/`LABELS`, or the
  Settings UI silently omits the tab.
- `review/app.py` and its templates are orphaned legacy Jinja code — no CLI command wires them.
  `review/report.py` and `templates/report.html.j2` are unrelated: they are the static-HTML
  `report` generator and stay.
