# CLAUDE.md

## What this is

Synopticon supplements Synology Photos' face recognition: it syncs a photo library from a NAS, runs an ensemble face pipeline (SCRFD + YOLO detection, ArcFace/AdaFace/MagFace embeddings), clusters faces, cross-references clusters against Synology's existing person labels, and writes approved corrections back through Synology's undocumented Person API. Quality-first, CPU-first (optional CUDA GPU), batch/offline — runtime is explicitly not a constraint; resumability is.

## Where the reasoning lives

This file carries the **rules**. `docs/adr/` carries the **decisions behind them** — the incident that motivated an invariant, the alternative that was tried and failed, the measurement that justifies a cache. Sniff that folder whenever you need the *why*, and always before reopening a design choice or weakening a rule.

| ADR | Read before touching |
|---|---|
| `01_offline-clustering-boundary.md` | `cluster/`, `cli.py` wiring, or adding an import to a leaf module |
| `02_synology-api-reverse-engineering.md` | `syno/`, any request payload, deep links, similar-photo groups |
| `03_ensemble-face-pipeline.md` | `pipeline/`, model preprocessing, the model manifest |
| `04_content-hashing-and-dedupe.md` | `sync/hashes.py`, `dedupe*.py` |
| `05_nas-write-safety-model.md` | anything that writes to the NAS, `JOB_SPECS`, consent gates |
| `06_job-subprocess-execution.md` | `progress.py`, `web/jobs.py`, adding progress to a long command |
| `07_web-process-responsiveness.md` | `web/app.py`, any route, middleware, SSE, or per-request query |
| `08_vue-spa-frontend.md` | `frontend/`, the SPA build, polling, review or job UI |
| `09_dual-database-backend.md` | `db/`, a migration, or any SQL |
| `10_docker-image-variants.md` | `docker/`, the publish workflow, deployment docs |
| `11_web-authentication.md` | `web/auth/`, sessions, API keys, the middleware's auth branch |
| `12_in-process-cron-scheduling.md` | `cron.py`, `web/schedule*` |
| `13_backup-downloads.md` | `web/backup_routes.py`, `db/snapshot.py`'s `SNAPSHOT_EXCLUDE`, `configio.export_config` |
| `14_review-retargeting.md` | `review/queries.py` mutations, the `hidden` status, `_existing_identities` |
| `15_orphaned-review-items.md` | `prune-queue`, `regen_crops`' skip logic, anything reading `face_id` out of `payload_json` |
| `16_inspect-photo-view.md` | `web/inspect_routes.py`, the Inspect page, photo links in the SPA, box geometry, the pan/zoom stage, tagging a face from Inspect |
| `17_web-security-hardening.md` | `web/clientip.py`, `web/auth/throttle.py`, `web/auth/authlog.py`, `web/auth/twofactor.py`, `web/auth/sessions.py`, `web/totp.py`, `web/security_routes.py`, the `[security]` config section, anything keying a decision on a client address |

Also read: `docs/GLOSSARY.md` before naming a new domain concept (it explains the ML vocabulary for engineers new to face recognition); `docs/agents/issue-tracker.md` before filing or picking up an issue or spec; `docs/agents/triage-labels.md` before setting a `Status:`; `docs/agents/domain.md` before writing a `CONTEXT.md` or a new ADR.

## Commands

```bash
uv sync --extra cpu --extra review --extra faiss   # dev setup (cpu/gpu are mutually exclusive; torch extras are opt-in)
uv sync --extra postgres                     # optional PostgreSQL backend
uv run pytest tests/unit/ -q                 # full suite, fully mocked, fast
uv run synopticon --help                     # command list
cd frontend && npm ci && npm run build       # build the Vue SPA once → src/synopticon/web/dist (Node 22+)
uv run --extra review synopticon web         # web GUI — needs [review] extra + a built frontend
uv run synopticon check                      # fast read-only NAS connectivity + auth probe
```

- The `cpu` and `gpu` extras share one import namespace and are declared conflicting, so `--all-extras` does not resolve — pick exactly one. A plain `uv run` re-syncs the venv to the default extras, so torch disappears again; pass the extra every time.
- Run the ONNX export scripts in an isolated environment, not via `--extra export` — see ADR 03 for the command and the reason.
- Tests must never contact a real NAS; all HTTP is mocked with respx. Live verification is manual, via `synopticon check` / `sync` (read-only).

## Architecture

Five CLI phases with **one database as the only contract between them** (schema in `src/synopticon/db/schema.sql`, migrations appended to `_MIGRATIONS` in `db/store.py`).

```
syno/ + sync/  ->  pipeline/   ->  cluster/      ->  review/       ->  syno/writeback.py
(NAS API,          (detect,        (kNN graph,       (HTML report,     (apply approved
 sync, download)    align, embed)   CW/HDBSCAN,       FastAPI UI)       items to NAS)
                                    crossref)
```

**Hard module boundary:** `cluster/` must never import from `syno/` or `pipeline/`. `pipeline/runner.py` takes a `fetch_original(row) -> Path` callable instead of importing `sync/`. `dedupe.py` is NAS-free; `dedupe_writeback.py` is its only NAS-touching half. `cli.py` is where the layers get wired together.

**Leaf modules stay dependency-free** so anything may import them: `cpu.py`, `progress.py`, `cron.py`, `links.py`, `pipeline/version.py`. Deep links are built in exactly one place — `links.py`. (ADR 01)

### Database layer (`db/`)

SQLite (`data/synopticon.db`) is the default and needs no configuration; PostgreSQL is opt-in via `[database] backend = "postgres"` plus the `[postgres]` extra. MySQL/MariaDB are out of scope. (ADR 09)

- **Write SQL in SQLite's dialect, always**, with `?` placeholders — `db/dialect.py` translates per backend. `json_extract` and the null-safe `IS`/`IS NOT` comparison are translated; `strftime`/`printf`/`group_concat` are not, so do that work in Python.
- **A migration is written once, in SQLite's dialect**, and appended to `_MIGRATIONS`. A `.pg.sql` name makes it PostgreSQL-only (it still consumes a version on both) — for a repair SQLite never needed, like widening int4 columns.
- **Catching a database error means calling `rollback()` before reusing the connection.** PostgreSQL aborts the whole transaction where SQLite shrugs it off; the `rollback()` is a no-op on SQLite. Driver exceptions arrive as `db.errors.*`.
- **A lost PostgreSQL session heals itself, but only at a transaction boundary.** `Connection` re-acquires from the pool and replays the statement when the current transaction has written nothing; past that the transaction is the caller's to redo, so a batch loop must commit per item and `rollback()` before continuing. Never widen the replay rule to a dirty transaction — that commits a fragment of one.
- **A row iterates values, not keys** — it is deliberately not a `Mapping`, because `lookups.fingerprint` depends on `tuple(row)`.
- **Every caller must `close()`** — under PostgreSQL that is what returns the connection to the pool.
- `store.connect(settings)` is the normal call; `store.describe(settings)` names the database for a human without leaking credentials.
- **`web_totp`, `web_recovery_codes`, `web_login_challenges` and `web_auth_log`** (migration `0010_web_security.sql`) hold two-step sign-in, its backup codes, an in-progress second sign-in step, and the sign-in log, respectively. `web_totp.secret` is the one plaintext bearer credential in the schema — see `db/snapshot.py::SNAPSHOT_EXCLUDE` before adding anything else that shouldn't leave the box in a backup. `web_users.session_pin_mode` and `web_sessions.pin_mode`/`pin_hash` (same migration) are session pinning's setting and its per-session enforcement datum — the two are denormalised on purpose, and `synopticon session-pin` has to clear both. (ADR 17)

### Synology API layer (`syno/`)

- **Discover API versions at runtime** via `SYNO.API.Info` (cached in `sync_state`). Hardcoding a version pins the client to one firmware.
- **Encode params through `client.encode_params`**, wrapping strings that need quoting in `QuotedString`. Getting this wrong fails *silently*.
- **Derive write payloads from the HAR captures in `./har/`** — consult them before changing a payload shape.
- `delete_face` **hard-deletes** a detection; `separate` moves a face and is reversible; `show` only flips visibility. Photo deletion is a different API and returns *queued*, not *done*.
- `SYNO.Foto.*` (personal) and `SYNO.FotoTeam.*` (shared) are mirrored; every row carries a `space` and `client.api_name(space, suffix)` picks the namespace.
- Resolve deep links through `db/store.py::link_photo_id`, or a link to a stacked photo lands on the homepage. (ADR 02)

### Pipeline (`pipeline/`)

- `pipeline_version` (model manifest + detection config) gates re-extraction; per-photo state is in `extract_log`. Each photo is one atomic transaction, so crash-resume is by construction.
- **Leave each embedder's preprocessing alone** — ArcFace, AdaFace and MagFace have genuinely different input contracts, and "harmonizing" MagFace inverts its embedding space.
- Clustering always uses `variant='orig'`; restored-face embeddings are advisory.
- **A crop is two artifacts: the images and the `faces` row pointing at them.** Only the row is read downstream — the review UI maps `crop_path` to a URL and never stats the disk — so a skip check that asks only the filesystem leaves crop-less faces unrepairable. `regen_crops` treats a NULL column as work and repairs it with a bare UPDATE, no fetch. (ADR 15)
- **A skipped photo must say why**: classify through `runner.skip_reason(exc, filename)` so the reason is plain language and lands in the run-level tally.
- Pin model weights by sha256 to versioned release URLs, never `releases/latest`. Weights are never committed. (ADR 03)

### Content hashes and dedupe

- **Compare `phash` by hamming distance on the bits, never by string equality** — byte-identical detection is `sha256`'s job.
- Keep the hash columns out of `sync_items`' upsert SET list, so a regular sync never invalidates them.
- `sha256 IS NULL` means "not yet hashed"; `phash IS NULL` does not — an undecodable image gets one without the other. (ADR 04)

### Config (`config.py`)

Precedence: init kwargs > env vars (`SYNOPTICON_<SECTION>__<KEY>`) > `.env` > TOML. Search order: `$SYNOPTICON_CONFIG`, `./config.toml`, `./data/config.toml`, `/data/config.toml`.

- **Credential fields must be `SecretStr`**, or `configio`'s masking does not cover them.
- Adding a config section means adding it to `frontend/src/utils/schema.ts`'s `SECTIONS`/`LABELS`, or the Settings UI silently omits the tab. `[security]` (below) is the reference example: it was added to both when it landed.
- Field help is two-tier: `description=` is the plain-language half, `json_schema_extra={"details": ...}` the technical half.

`[security]` (`SecurityConfig`, ADR 17) governs who can reach the GUI and how hard it is to guess a
password into it: `trusted_proxies` and `allow_from`/`allow_private_networks` (the network side —
see `web/clientip.py`); `max_failures_per_address`/`address_window_minutes`/`address_block_minutes`
(login throttling); `sign_in_log`/`sign_in_log_days`/`sign_in_log_max_entries`/`log_failed_api_keys`
(the sign-in log); and `totp_issuer`/`totp_skew_steps`/`login_challenge_ttl`/`login_code_attempts`/
`recovery_code_count` (two-step sign-in). Every field in it is read once at start-up — a save always
tells the operator to restart — except the two-step and pin CLI recoveries, which act on the
database directly and need no restart (see ADR 11's lockout-recovery table). `trusted_proxies` and
`allow_from` share one validator (`_valid_networks`, `ipaddress.ip_network(strict=False)`), so an
invalid entry in either fails the same way.

### Safety model (do not weaken)

Everything before `apply` is read-only toward the NAS. `apply` is dry-run by default, with a flag per danger tier:

| Tier | Gate |
|---|---|
| assign / low_confidence / new_person | `--apply` |
| reassign | `--apply --apply-reassigns` |
| merge (one side unnamed) | `--apply --apply-merges` |
| merge_named (both named — destroys a human label) | `--apply --apply-merges-named` |

`--apply-merges` never covers `merge_named`. Only `status='approved'` rows are eligible, every attempt lands in `audit_log`, and an idempotency pre-check re-fetches NAS state before each write.

- **The GUI must never pass `-Y` or use `apply-all`** — `_FORBIDDEN_TOKENS` enforces it. Commands are never raw argv: `JOB_SPECS` whitelists params, and `validate_consent` is the sole place apply flags are appended.
- **`eval`, `reset-password`, `db-migrate`, `disable-2fa`, `web-access` and `session-pin` stay CLI-only** — never give any of them a `JOB_SPECS` entry. Only `web-access` needs a restart to take effect; the others act on the database directly. (ADR 11, ADR 17)
- Schedules replay a stored submission with `confirm_phrase` always `None`, so every typed-phrase job is unschedulable by construction.
- QuickMerger is the one GUI surface that writes outside `apply`; it refuses a named↔named merge with 409. (ADR 05)
- The review UI's Hide / Merge into / Reassign actions rewrite `review_queue` and never call the NAS, which is why they carry no consent gate. Keep it that way. (ADR 14) Inspect's per-face **Tag as… / Reassign…** is the same trade — but **the server picks the kind, never the client**: `queries.assign_face` writes `reassign` (and its stricter flag) when `photo_syno_match` finds a Synology box already named on that face, and plain `assign` otherwise. Taking a kind from the request body would be a way to write a reassign under `--apply`. (ADR 16)

### Review queue statuses

`pending | approved | rejected | hidden | applied | failed`. Two of them are easy to conflate:

- **`rejected` is re-proposed by the next `cluster` run; `hidden` is not** — `_existing_identities` counts `hidden` as seen. That is the only difference, and it is the point of having both.
- **`approved` does not imply the pipeline proposed it.** A retarget writes an approved row from a human's pick; `payload.manual_target` marks those, and their `confidence` is NULL because the stored score belonged to the person that got overruled (`payload.original_person_id`).

**`payload_json`'s face ids have no foreign key, and a re-extract renumbers them** (`faces.face_id` is AUTOINCREMENT; `_process_photo` deletes and re-inserts a photo's faces). Rows proposed before a `pipeline_version` bump therefore point at ids that are gone, render with no crop, and cannot be repaired by `regen-crops` — the bbox they would be rebuilt from went with the old rows. `queries.orphaned_items` finds them and `prune-queue` deletes them; **deleting is the point, never `hidden`**, which `_existing_identities` counts as seen and would suppress the correct re-proposal forever. An orphan is only an orphan when *every* face it names is unrecoverable. (ADR 15)

### Web GUI (`web/`)

A Vue 3 + TS SPA (Vite) served by a FastAPI backend behind the `[review]` extra. `dist/` is gitignored — build it before `synopticon web`, and keep the `artifacts` entry in `[tool.hatch.build.targets.wheel]` or the SPA drops out of wheels. (ADR 08)

**Responsiveness invariants — one uvicorn process, so one blocking call stalls everything.** Read ADR 07 before touching any route, and keep all four:

1. **No blocking I/O on the event loop.** An `async def` handler does its SQLite/filesystem/network/scrypt work inside `run_in_threadpool`.
2. **SSE handlers are async generators**, never sync ones.
3. **The web process never imports `pipeline.runner`**, nor anything pulling `cv2`/`onnxruntime` at module scope.
4. **Every middleware is pure ASGI**, never `@app.middleware("http")`. Last added is outermost, so registration order is load-bearing.

`GET /api/health` is the only endpoint a container healthcheck may point at — keep it connection-free and thread-free.

Other things that are easy to regress:

- **Inspect is the one read-only surface that writes**, and only to `review_queue`: `assign_face` queues an approved row for a face nothing proposed, superseding any queued row that speaks for that face *alone* (to `hidden`, never deleted — `_existing_identities` has to keep counting the overruled pair).
- **Inspect's image route is the web process's only photo fetch** — a NAS thumbnail proxy that reuses QuickMerger's `NasSession`, so it is registered after it. Box geometry is reconciled server-side by `inspect_routes.resolve_frame`: our detections are pixels in the EXIF-corrected frame, Synology's are 0..1 of its upright one. **`photos.width`/`height` are never swapped on `orientation`** — Synology reports the frame it displays, that swap measured wrong on ~90% of rotated photos, and `crossref`/`_face_targets` have always read them unswapped. `resolve_frame` scores every (frame, quarter-turn) pair against Synology's boxes, requiring a clear margin over the seed so a centred face's half-turn symmetry cannot flip a good frame. The turn is reported as `display.rotation` and applied by the SPA to **our** boxes only, never by turning the photo. The stage is a pan/zoom viewport (`usePanZoom`): **only the photo is transformed** — boxes are placed over it in viewport coordinates (`x * scale` percent plus the pan offset), because a scaled overlay both rasterizes soft and re-creates the label overlap the zoom exists to fix. (ADR 16)
- **A photo link inside the SPA goes to Inspect (`inspect_url`, the raw photo id); `item_url` is for links that leave the app** — the standalone report, job logs, CLI output — and targets the similar-group top pick. Both are built in `links.py`.
- **`quickmerger.py`, `schedule_routes.py`, `backup_routes.py` and `inspect_routes.py` must not carry `from __future__ import annotations`** — FastAPI would degrade their `Request` params to required query fields (422 on every call).
- Any new per-request map derived from the whole library needs a fingerprint-keyed cache, not a recomputation.
- Any new poller must be `setTimeout`-chained with an in-flight guard, error backoff and `visibilitychange` gating. Subscribe to the existing `stores/jobs.ts` poller rather than adding a timer.
- Every view that runs a job mounts the shared `JobPanel`. Don't build a second one. Same for
  picking a person: `PersonPickerDialog` takes a described source (label, thumbnails, space,
  optional note), not a review item, so any surface can point it at faces.
- "The client address is only trustworthy behind `[security] trusted_proxies`, and only when that proxy *overwrites* `X-Forwarded-For`. `clientip.ProxyHeaders` is the sole place `X-Forwarded-*` is honoured, it is the outermost middleware, and it *adds* `scope['synopticon.client']` rather than overwriting `scope['client']`; uvicorn's own `proxy_headers` is passed `False` explicitly, because its default is `True` trusting loopback. Everything that keys on an address goes through `clientip.client_ip`, everything that *punishes* an address goes through `clientip.ip_prefix`, and anything that needs the connection itself goes through `clientip.client_peer`."
- "No defence is conditional. Every throttle tier is armed on every request, keyed on the one resolved address; `clientip` exposes no `local_request`, no `stands_in_for` and no `address_tiers_armed`, and `LoginRateLimiter.verdict` reads exactly one field of its client argument. Three separate drafts added an exemption for 'the server itself' and all three switched login throttling off for the entire internet on the deployment this README recommends. Do not add a fourth."
- "Loopback is allowed by the allowlist unconditionally and throttled unconditionally. That asymmetry is deliberate: the allowlist is a door whose recovery path arrives on loopback, and the throttle is a speed bump whose worst case is a five-minute wait."
- "In-process state shared between the event loop and the AnyIO pool takes a lock — `LoginRateLimiter._lock`, `EventThrottle._lock`, `authlog._state_lock`, `auth_cache_lock` — and no lock is ever held across a database call or a scrypt derivation."
- "A database snapshot must never carry a bearer credential. `db/snapshot.py::SNAPSHOT_EXCLUDE` is why the two-step-sign-in tables are missing from a backup, and it is deliberately not `db/copy.py::TABLES`, which `db-migrate` shares."

(ADR 17.)

### Jobs and progress

- Instrument long loops with `phase()` / `progress()` — heartbeat narration, rate and ETA come for free. Keep `progress()`'s phase name identical to the `phase()` that opened it.
- Existing `log.info`/`log.warning` calls already become job-log events via the log bridge; that is the intended way to add commentary.
- **Read `stdout.log`/`stderr.log` with `newline=""`** — universal-newline translation breaks the `\r` collapse.
- **Core counts come from `cpu.py`**, never `os.cpu_count()` or `/proc/cpuinfo` — neither is namespaced, so a 2-core container would size a 31-thread BLAS pool. (ADR 06)

## Conventions

- **User-facing prose says "Detect faces" / "Group faces", never "extract" / "cluster".** The ML vocabulary stays in code, schema and CLI *command names* — those are the stable contract for scripts, `JOB_SPECS` keys, `SCHEDULABLE` keys, `schedules.job` rows and the `[clustering]` config section. Everything a homelabber reads is translated, including CLI `--help`. A job id reaching the DOM goes through `frontend/src/utils/jobs.ts`'s `jobLabel()`. Progress **phase** names are protocol identifiers and stay untranslated.
- Python 3.11+ (`>=3.11,<3.13`); `numpy>=2.4.6,<3` pinned.
- New CLI commands go in `cli.py` with lazy imports inside the command function, to keep CLI startup fast.
- Add comments only for something arcane.
- This project is fluid. When a solution no longer works, adapt it — if you find yourself planning an elaborate workaround, change the architecture instead.
- **Keep the docs honest as you go.** README is the FOSS onboarding path: how to run the project first, current architecture second, never past decisions. Update it when CLI, config or Docker layout changes, and update this file when commands or the data model change. A decision worth remembering goes in a new `docs/adr/` file, not here and not in the README.

## Tasks

- **Version bump:** adjust the version in `./pyproject.toml` and `./frontend/package.json` per the given strategy (major/minor/patch), then run `uv sync` and `cd frontend && npm install` to sync the lockfiles.
