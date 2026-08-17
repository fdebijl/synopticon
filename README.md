![Synopticon logo](assets/Synopticon%20Hero.png)

Synopticon is a toolkit to run alongside Synology Photos, consisting of a set of standalone utilities that run alongside your library and take on the jobs DSM currently does poorly or not at all. The current toolkit includes enhanced face recognition, robust face grouping, automatic face reassignment, duplicate photo deletion and much more.

## Features

**Enhanced face recognition**  
 Synology's built-in face detection misses faces and sometimes links photos to the wrong person. Synopticon runs a frontier ensemble pipeline (multi-scale SCRFD + YOLO-face detection, ArcFace + AdaFace + MagFace embeddings, graph clustering) over your library, cross-references the results against what Synology already knows, and writes corrections back through Synology Photos' Person API. Writebacks only occur after your explicit review.

![](./assets/pretty_screenshots/review.png)


**QuickMerger**  
Quickly work through unnamed faces in Synology Photos through an ergonomic merge interface, perfect for giving the initial set of faces a name to start working with inside Synopticon.

**Deduplication**  
finds duplicate photos (byte-identical, or visually near-identical) from content hashes computed over your originals, keeps the highest-quality copy of each group, and deletes the rest through Synology's background-task API.

![](./assets/pretty_screenshots/quickmerge_dedupe.png)

**Scheduled Runs**  
After the initial setup, schedule synchronization with the NAS, face detection and face grouping without having to touch Synopticon. Just come back every now and then to review the groups.

![](./assets/pretty_screenshots/scheduler.png)

## Quickstart - Web GUI

Running `synopticon web`, either directly from the repo or inside a Docker container, serves a full browser GUI that handles everything Synopticon can do: first-run setup, editing your config, running every pipeline/maintenance command with live progress, working the review queue, and applying corrections. It's the recommended way to drive Synopticon day to day, but the CLI is always there for scripting and headless runs.

The GUI is a Vue 3 single-page app served by the backend. For hosting the GUI, Docker is the recommended path. The image ships the frontend prebuilt, so you don't need Node or any of the other deps on the host:

```bash
docker compose run --service-ports synopticon web --host 0.0.0.0
```

Once the container is up, point your browser http://127.0.0.1:8686 and follow the setup guide.

Note: `--host 0.0.0.0` is required in a container: the `127.0.0.1` default binds the container's own loopback, which no published port can reach. The host-side binding stays on `127.0.0.1` either way — see [Docker images](#docker-images) for the plain `docker run` form.

If you'd rather run it locally, you'll have to build the SPA once (with Node 22+), then the same `uv run synopticon web` to serve it up:

```bash
uv sync --extra cpu --extra review --extra faiss   # backend GUI deps (fastapi, uvicorn, tomlkit) - use --extra gpu instead if you have a supported GPU
cd frontend && npm ci && npm run build             # builds the SPA into src/synopticon/web/dist (Node 22+)
cd .. && uv run synopticon web                     # http://127.0.0.1:8686
```

For working on the UI, run the two dev servers side by side: `uv run synopticon web` (backend on :8686) and, in another terminal, `cd frontend && npm run dev` (Vite on :5173, hot-reload). Vite proxies `/api` and `/crops` to the backend, so open the app at `http://127.0.0.1:5173` and the session cookie works same-origin.

### Schedules

**Schedules** puts recurring work on a cron timer without a cron daemon in the image — the intended setup for a container, where `synopticon web` is the only long-running process. Pick a job, fill in the same parameters the Pipeline/Utilities/Apply pages expose, give it a 5-field cron expression (or one of the presets), and optionally a timezone; the form previews the next five firings as you type.

```
minute  hour  day-of-month  month  day-of-week
0       3     *             *      *            # nightly at 03:00
0       */6   *             *      *            # every six hours
0       4     *             *      SUN          # Sunday mornings
```

Ranges, lists, steps (`*/15`, `1-5`, `0,30`), month/day names, and the `@hourly`/`@daily`/`@weekly`/`@monthly`/`@yearly` macros all work. Times are the server's local zone (set `TZ` in the container) unless you name one per schedule.

- Schedulable: `sync`, `extract`, `cluster`, `recluster`, `regen-crops`, `report`, `apply`, `dedupe` (dry run), `clear-queue`, `delete-crops`.
- Not schedulable: anything behind a typed phrase. Named↔named merges, `dedupe --apply` and `reset --all` require a human to type the phrase at that moment, which a timer cannot do — the server refuses to save such a schedule at all. `reset` is left off the list entirely.
- A firing is skipped when the same command is still in flight (a long `extract` never stacks) and missed — recorded, not backfilled — for occurrences that fell while the server was down.
- Each schedule keeps a short history of its firings, linking to the job log of each run. Run now fires one immediately without disturbing the next scheduled time.

Running the GUI bare-metal rather than in a container? Plain `cron` calling the CLI works just as well and survives the server being stopped.

### Authentication

- Admin account — one username/password, created in the wizard, stored scrypt-hashed. Sessions are HttpOnly, SameSite=Lax cookies (30-day expiry, surviving restarts). Change the password from Settings → Access, or — if you've locked yourself out — from the shell with [`synopticon reset-password`](#reset-password).
- API keys — create named, revocable keys under Settings → Access (shown once, then stored hashed). Send them as `Authorization: Bearer syn_...` on `/api/*` requests — the intended path for automation and planned sidecars (e.g. a browser extension). Cookie endpoints are CSRF-hardened (JSON-only + SameSite); the bearer path is immune by construction.

```bash
curl -H "Authorization: Bearer syn_xxxxxxxxxxxxxxxx" http://127.0.0.1:8686/api/stats
```

### Reverse proxy / TLS

Synopticon does not terminate TLS itself. Bind it to loopback (the default `127.0.0.1`) and put nginx, Caddy, or Traefik in front for HTTPS. uvicorn runs with proxy headers enabled, so it honours `X-Forwarded-Proto` from the proxy — the session cookie's `Secure` flag then follows the effective scheme automatically. A minimal Caddy example:

```
photos.example.com {
    reverse_proxy 127.0.0.1:8686
}
```

## Quickstart - Docker

Running docker locally is perfect if you don't have a separate Docker box to run the container on, or only wish to use the tools in Synopticon intermittently.

```bash
git clone https://github.com/fdebijl/synopticon && cd synopticon
mkdir -p data models
cp config.example.toml data/config.toml      # edit: set [nas] url
export SYNOPTICON_NAS__URL=https://your-nas.example.com
export SYNOPTICON_NAS__ACCOUNT=photos-bot     # a dedicated NAS user is recommended
export SYNOPTICON_NAS__PASSWORD=...

docker compose build                          # or skip: see Docker images below to pull instead
docker compose run synopticon models download
docker compose run synopticon sync
docker compose run synopticon extract          # the long pass; resumable, re-runnable (see GPU acceleration below)
docker compose run synopticon cluster
docker compose run synopticon report           # static HTML in ./data/report/<run>/
docker compose run --service-ports synopticon web --host 0.0.0.0   # GUI at http://127.0.0.1:8686 (review lives at /review)
docker compose run synopticon apply            # dry-run; add --apply to write
```

> `web` binds `127.0.0.1` by default, which inside a container means the container's *own* loopback — unreachable from the host however you publish the port. `--host 0.0.0.0` binds all container interfaces; the compose file still only publishes to `127.0.0.1:8686` on the host, so it stays off your network.

Deduplication is a separate, shorter flow off the same `sync`:

```bash
docker compose run synopticon sync --hash       # one-time: hash every original (slow, resumable)
docker compose run synopticon dedupe --exact     # dry-run; add --apply to delete
```

Where state lives: everything is under the repo root in both flows — `data/` (SQLite db, face crops, reports, originals cache) and `models/` (ONNX weights). Inside the container those directories appear as `/data` and `/models` (the compose file mounts them), but on disk it's the same `./data` and `./models` either way, so you can freely mix Docker and bare-metal runs against the same state.

Without Docker: `uv sync --extra cpu` (or `--extra gpu` for CUDA — see [GPU acceleration](#gpu-acceleration)), then `cp config.example.toml config.toml` (edit `[nas]`; the storage defaults already point at `./data` and `./models`) and `uv run synopticon <command>` (Python 3.11/3.12). The browser GUI additionally needs the frontend built once — `cd frontend && npm ci && npm run build` (Node 22+); see [Web GUI](#web-gui). The CLI itself needs no Node.

Prefer a GUI? `synopticon web` drives everything below from the browser — including a guided first-run wizard, so you can skip the manual config edit and CLI dance. See [Web GUI](#web-gui).

### Docker images

Prebuilt images are published to Docker Hub at [`fdebijl/synopticon`](https://hub.docker.com/r/fdebijl/synopticon), so you can skip `docker compose build` entirely. You can pick from two tags: `cpu` and `gpu`. If you have an NVIDIA GPU, it's highly recommend to use the `gpu` image to drastically speed up extraction. See [GPU acceleration](#gpu-acceleration) for further details.

| Tag | Variant | Moves? | Use it for |
|---|---|---|---|
| `latest` | CPU | on each release | Trying it out. `latest` is the CPU build deliberately — it's the variant that runs anywhere. |
| `cpu` | CPU | on each release | Same image as `latest`, but explicit. Prefer this in a compose file so the variant is legible. |
| `gpu` | CUDA | on each release | NVIDIA hosts. Needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). |
| `0.1.0`, `0.1.0-cpu` | CPU | never | Pinning to an exact release. |
| `0.1.0-gpu` | CUDA | never | Pinning to an exact release. |
| `0.1`, `0.1-cpu`, `0.1-gpu` | both | tracks patches | Auto-picking up `0.1.x` fixes without jumping a minor. |
| `sha-<short>-cpu`, `sha-<short>-gpu` | both | never | Reproducing one exact commit; also what unreleased manual builds publish. |

```bash
docker pull fdebijl/synopticon:cpu

docker run --rm \
  -v "$PWD/data:/data" -v "$PWD/models:/models" \
  -e SYNOPTICON_NAS__URL=https://your-nas.example.com \
  -e SYNOPTICON_NAS__ACCOUNT=photos-bot \
  -e SYNOPTICON_NAS__PASSWORD=... \
  fdebijl/synopticon:cpu sync
```

The GUI additionally needs a published port and `--host 0.0.0.0`:

```bash
docker run --rm -p 127.0.0.1:8686:8686 \
  -v "$PWD/data:/data" -v "$PWD/models:/models" \
  fdebijl/synopticon:cpu web --host 0.0.0.0
```

On a GPU host, swap the tag and add `--gpus all`:

```bash
docker run --rm --gpus all \
  -v "$PWD/data:/data" -v "$PWD/models:/models" \
  fdebijl/synopticon:gpu extract
```

Using official images with compose. The bundled `docker-compose.yml` builds from source. To pull instead, drop the `build:` block and name a tag. You can optionally add a `docker-compose.override.yml` to do this without touching docker-compose.yml:

```yaml
services:
  synopticon:
    image: fdebijl/synopticon:cpu
    build: !reset null
  synopticon-gpu:
    image: fdebijl/synopticon:gpu
    build: !reset null
```

### Healthchecks

The image ships without a built-in `HEALTHCHECK`. Add one on the service when you run `web` as a managed service (Swarm, Portainer, `docker compose up`):

```yaml
healthcheck:
  # The slim image has no curl; use the bundled python.
  test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8686/api/health', timeout=5).status==200 else 1)"]
  interval: 30s
  timeout: 10s
  retries: 5
  start_period: 60s
```

## CLI reference

All commands are subcommands of `synopticon`: 
- Docker: `docker compose run synopticon <command>`
- Local: `uv run synopticon <command>`

### Setup & diagnostics

#### `check`
Verify NAS connectivity and credentials (read-only, fast). No options.

#### `hwinfo`
Print hardware/environment stats relevant to face detection and grouping — CPU model and core count, RAM, ONNX Runtime version and available execution providers (i.e. whether a GPU is usable), detected NVIDIA GPUs, key library versions, which model weights are present, and disk space. Touches nothing and reaches no network; include its output when filing a bug report. No options.

#### `models download`
Download and verify model weights into `storage.models_dir`.

| Option | Default | Description |
|---|---|---|
| `--only TEXT` | all | Subset of model keys to fetch. Repeatable. |
| `--allow-record-hash` | off | Record the sha256 of not-yet-pinned (locally exported) models. |

#### `benchmark`
Measure face-detection throughput on your hardware without changing anything. Reuses the `extract` pipeline (detect → align → embed) over a sample of photos but writes no faces, embeddings, or crops. Reports photos/sec, faces/sec, ms/photo, and a per-stage breakdown so you can see where time goes. Originals are downloaded and cached exactly as `extract` would. A short warmup pass runs first so one-off ONNX session/thread-pool startup cost doesn't skew the numbers. Pair with `hwinfo` when tuning `[inference]` threads or comparing CPU vs GPU.

| Option | Default | Description |
|---|---|---|
| `--limit INTEGER` | 25 | Number of photos to time. |
| `--photo-id INTEGER` | none | Benchmark a single photo id. |
| `--space TEXT` | `personal` | Space to pull benchmark photos from. |
| `--warmup INTEGER` | 2 | Photos to process before timing (absorbs ONNX startup cost). |

### Sync

#### `sync`
Pull photos, persons, and ground-truth labels from the NAS. Prints live progress per phase (an in-place line on a terminal, periodic lines when piped/in Docker logs). Feeds both the face pipeline and `dedupe` (via `--hash`).

| Option | Default | Description |
|---|---|---|
| `--space TEXT` | all configured | Limit to one space. |
| `--skip-faces` | off | Skip the per-photo face-level ground-truth pass. |
| `--all-faces` | off | Fetch `list_face` for ALL photos, not just those already tagged. |
| `--resume` / `--no-resume` | `--resume` | Resume the faces pass from its saved cursor; `--no-resume` forces a full re-scan. |
| `--hash` | off | Also download each image and store its sha256 + 64-bit perceptual hash (DCT pHash) on the `photos` table. Required before `dedupe`. |

The faces pass is checkpointed per-photo, so it resumes cleanly after an interruption. The items and persons passes are idempotent but restart from the top.

`sync` also records Synology's "similar photo" (stacking) grouping, so review and dedupe deep links point at the group's visible cover photo instead of a non-top-pick member the grouped timeline hides. A failure here (e.g. the feature being unavailable on a space) is logged as a warning and never aborts the rest of `sync`.

`--hash` downloads every image original (videos are skipped), so the first pass is slow on a large library; it commits per photo and skips already-hashed photos on re-runs, re-hashing only photos whose Synology `cache_key` changed (i.e. edited/replaced). Cached originals are evicted afterward unless `storage.keep_originals` is set. Compare `phash` values by hamming distance, not equality — byte-identical duplicates are the `sha256` column's job.

### Face recognition

#### `extract` — *Detect faces*
Scan synced photos, record every face found, and compute ensemble embeddings (resumable; interrupt any time). Evicts cached originals afterward unless `storage.keep_originals` is set.

| Option | Default | Description |
|---|---|---|
| `--limit INTEGER` | none | Process at most N photos. |
| `--photo-id INTEGER` | none | Process a single photo id. |
| `--space TEXT` | all configured | Limit to one space. |

#### `cluster` — *Group faces*
Group all detected faces by person and cross-reference them against Synology persons. Writes a new grouping run. No options.

#### `recluster` — *Re-group faces*
Group the faces again from the cached embeddings with parameter overrides. Never hits the NAS.

| Option | Default | Description |
|---|---|---|
| `--set SECTION.KEY=VALUE` | none | Override a config value, e.g. `--set clustering.edge_threshold=0.47`. Repeatable. |

#### `report`
Generate the static HTML review report.

| Option | Default | Description |
|---|---|---|
| `--run-id INTEGER` | latest | Grouping run to report on. |

#### `web`
Serve the full web GUI (dashboard, pipeline, review, apply, maintenance, settings) with a first-run setup wizard. Under Docker, add `--service-ports` and `--host 0.0.0.0` — the default bind is unreachable from outside the container. See [Web GUI](#web-gui) for the wizard, auth, and reverse-proxy guidance.

| Option | Default | Description |
|---|---|---|
| `--host TEXT` | `127.0.0.1` | Bind address. Use `0.0.0.0` in a container. |
| `--port INTEGER` | `8686` | Bind port. |

#### `review` *(deprecated alias)*
Deprecated alias of `web`, kept for one release. Serves the same app and prints the `/review` URL; new usage should call `synopticon web`. Same `--host`/`--port` options.

#### `reset-password`
Reset a web GUI account's password from the shell — the recovery path when the admin password is lost. It rewrites the scrypt hash in the configured database directly, so it needs access to that database but no login, and it never touches the NAS. All of that account's sessions are revoked (so a stolen cookie can't outlive the credential) unless `--keep-sessions`. With a single account the username can be omitted. This is CLI-only by design and is not exposed as a web job.

```bash
uv run synopticon reset-password              # prompts twice, hidden
docker compose run --rm synopticon reset-password admin
```

| Option | Default | Description |
|---|---|---|
| `[USERNAME]` | the only account | Account to reset; required if several exist. |
| `--password TEXT` | *(prompt)* | New password non-interactively. Prefer the prompt — this lands in your shell history. |
| `--keep-sessions` | off | Leave that account's existing logins valid. |

#### `apply`
Apply approved review items to the NAS. Dry-run unless `--apply` is given.

| Option | Default | Description |
|---|---|---|
| `--kinds TEXT` | `assign,low_confidence` | Comma-separated kinds to apply. `assign` and `low_confidence` are the same reviewer-approved face assignment (they differ only in the pipeline's original confidence), so both apply by default; `merge` (an unnamed side is involved) is gated by `--apply-merges`; `merge_named` (joins two already-named people) is gated by the stricter `--apply-merges-named`; `reassign` (opt-in via `--kinds reassign`) is gated by `--apply-reassigns`. |
| `--person-id INTEGER` | none | Scope to a single person id (matches either side of a merge or reassign). |
| `--apply` | off (dry-run) | Actually write to the NAS. |
| `--apply-merges` | off | Extra gate required before an ordinary `merge` (at least one unnamed side) is written. |
| `--apply-merges-named` | off | Extra gate required before a `merge_named` — joining two people who *both* already have a name. Irreversible and destroys a human-assigned label, so it is deliberately not covered by `--apply-merges`. |
| `--apply-reassigns` | off | Extra gate required before any reassign is written — it moves an existing (wrong) Synology face label to a different person via a single reversible `Person.separate` call. |
| `--space TEXT` | `personal` | Space to write to. |
| `--report` | off | Print the audit trail afterward. |

Every run (including dry-runs) appends a per-operation trace to `apply.log` in the project root, one line per writer call — useful for confirming what actually reached the NAS.

#### `apply-all`
Apply all approved review items — assigns, low-confidence assigns, reassigns, *and* merges — in one go. Prints a per-kind count of what is about to be written and asks for confirmation; unlike `apply` there is no dry-run stage and the `--apply-merges`/`--apply-reassigns` gates are implicitly lifted. Merges are still irreversible — take a NAS snapshot before a large run.

The one exception is `merge_named` (joining two already-named people): every such pair is listed with a loud warning and gated behind a separate confirmation, so approving the bulk write never silently applies them. When running non-interactively (`-Y`), named→named merges are skipped unless you also pass `--apply-merges-named`.

| Option | Default | Description |
|---|---|---|
| `--yes`, `-Y` | off | Skip the confirmation prompt (for scripted runs). |
| `--apply-merges-named` | off | Include `merge_named` items. Required to apply them with `-Y`; interactively they get their own confirmation prompt instead. |
| `--person-id INTEGER` | none | Scope to a single person id. |
| `--space TEXT` | `personal` | Space to write to. |
| `--report` | off | Print the audit trail afterward. |

#### `eval holdout`
Mask a fraction of known labels, group the faces again, and measure recovery.

| Option | Default | Description |
|---|---|---|
| `--mask-fraction FLOAT` | `0.2` | Fraction of known labels to mask. |
| `--seed INTEGER` | `42` | Random seed. |

#### `eval grid`
Grid-search tunables; writes `eval_grid.csv` under `storage.data_dir`.

| Argument / Option | Default | Description |
|---|---|---|
| `GRID_JSON` (argument) | required | JSON of param→values, e.g. `'{"edge_threshold": [0.45, 0.5, 0.55]}'`. |
| `--mask-fraction FLOAT` | `0.2` | Fraction of known labels to mask. |
| `--seed INTEGER` | `42` | Random seed. |

### Deduplication

#### `dedupe`
Delete duplicate photos from the NAS using the content hashes `sync --hash` stores. Two independent levels (pass either or both); within each duplicate group the highest-resolution photo is kept (tie-break: largest file, then lowest id) and the rest are deleted. Hashes are trusted, so there is no review step — but it is dry-run by default and prints a Synology Photos deep link for every photo first, and `--apply` asks for confirmation before writing.

| Option | Default | Description |
|---|---|---|
| `--exact` | off | Delete byte-identical duplicates (grouped by `sha256`). |
| `--visual` | off | Delete visually near-identical duplicates (`phash` within `--threshold` bits; transitively grouped). |
| `--threshold INTEGER` | 5 | Max pHash hamming distance (0–64) for a `--visual` match; lower is stricter. |
| `--apply` | off (dry-run) | Actually delete from the NAS. |
| `--space TEXT` | all configured | Deduplicate within one space (groups never span personal/shared). |
| `--yes`, `-y` | off | Skip the confirmation prompt when deleting. |

Requires a prior `sync --hash` pass to populate the hashes. Each attempt (including dry-runs) is recorded in `audit_log` and `apply.log`. Deletion goes through Synology's `BackgroundTask.File.delete` API as a batched background task; verify on your DSM whether that moves photos to the recycle bin or hard-deletes them before trusting `--apply` on a large run — start with one known duplicate and check.

### Housekeeping

These commands manage locally-computed state and crop images. None of them touch the NAS (except `regen-crops`, which reads originals but never writes).

#### `reset`
Clear locally-computed data (faces, embeddings, face groups, and the review queue) plus their crop images, so the pipeline can rebuild from scratch. Useful after tweaking face detection/grouping settings: the review UI pools items from every run, so stale suggestions would otherwise linger. Synced NAS metadata is kept unless `--all` is given. Never touches the NAS.

| Option | Default | Description |
|---|---|---|
| `--all` | off | Also drop synced NAS metadata (photos, persons, ground truth); forces a full re-sync. |
| `--keep-crops` | off | Leave crop images on disk instead of deleting them with the faces. |
| `--yes` / `-y` | off | Skip the confirmation prompt. |

Typical use after changing `[detection]` scores: `synopticon reset` then `synopticon extract && synopticon cluster`.

#### `clear-queue`
Delete only the pending review-queue items so the next `cluster` run re-generates them from scratch. Pending rows regenerate cleanly — crossref re-inserts them, picking up any person names synced since the last run (handy when a face was suggested for a person who was still unnamed on the NAS when the faces were grouped). Approved, applied and rejected rows are the ledger crossref uses to avoid re-surfacing work you've already handled, so this command refuses to touch them; use `reset` if you truly want to rebuild everything. Never touches the NAS.

| Option | Default | Description |
|---|---|---|
| `--yes` / `-y` | off | Skip the confirmation prompt. |

Typical use after a `sync` fills in previously-missing person names: `synopticon clear-queue` then `synopticon cluster`.

#### `delete-crops`
Delete every face crop image from disk to reclaim space, leaving the `faces` table (bboxes, landmarks, embeddings) untouched. Crops are a pure derived artifact, so this is safe and reversible — rebuild them on demand with `regen-crops`. Never touches the NAS.

Crop images can accumulate to many gigabytes over a large library. If you run the pipeline intermittently and don't need the review report/UI between passes, wiping crops after each pass keeps disk usage down — the crops cost nothing to recreate from the cached `faces` rows (plus the originals, re-fetched from the NAS) when you next want to review.

| Option | Default | Description |
|---|---|---|
| `--yes` / `-y` | off | Skip the confirmation prompt. |

#### `regen-crops`
Rebuild face crop images from the stored bboxes/landmarks and the originals (re-fetched from the NAS), without re-running detection or embedding. Use it to recover from a `delete-crops` (or an accidental wipe), or to repair a partial one. Resumable — it commits per photo. Originals are evicted afterward unless `storage.keep_originals` is set. Reads the NAS but never writes to it.

| Option | Default | Description |
|---|---|---|
| `--space TEXT` | all | Limit to one space. |
| `--only-missing` / `--all` | `--only-missing` | Only rebuild crops whose files are missing (skips photos already fully on disk, so re-runs are cheap); `--all` rewrites every face's crops. |
| `--limit INTEGER` | none | Process at most N photos. |

#### `db-migrate`
Copy an existing library into the configured database backend — the one-time move you need after switching `[database] backend` to `postgres`, so the switch doesn't cost a fresh `sync` and a multi-hour re-`extract`. See [Database backend](#database-backend). Never touches the NAS.

The destination is whatever `[database]` currently selects; the source defaults to the SQLite file that backend replaced. It refuses to run if the destination already holds data, and it copies primary keys verbatim, so deep links, review decisions and the audit trail all survive.

| Option | Default | Description |
|---|---|---|
| `--from TEXT` | the SQLite file under `data_dir` | Database to copy out of (a path, or a `postgresql://` URL). |
| `--yes` / `-y` | off | Skip the confirmation prompt. |

Run it once, with nothing else touching either database.

### Database backend

By default Synopticon keeps its own data — the photo index, the faces it found, your review decisions — in a single SQLite file at `data/synopticon.db`. That needs no setup and is the right choice for almost everyone. (This is never your NAS; Synopticon only ever reads Synology's own database through its API.)

If you already run PostgreSQL, want the database on different storage from the photo cache, or want to back it up with the tools you already have, point Synopticon at it instead:

```toml
[database]
backend  = "postgres"
host     = "db.example.internal"
port     = 5432
user     = "synopticon"
database = "synopticon"
sslmode  = "require"
```

The password is best passed env-only: `SYNOPTICON_DATABASE__PASSWORD=...`. Managed providers that hand you a ready-made connection string can skip the individual fields and set `SYNOPTICON_DATABASE__URL` instead, which overrides them all.

**Setup:**

1. `uv sync --extra postgres` (or use a Docker image — the extra is not in the default install).
2. Create the database and a role that owns it. Synopticon creates its own tables inside it, but it will not create the database itself.
3. Set `[database]` as above. Synopticon applies its schema on first connection.
4. Moving an existing library across: `synopticon db-migrate`.

The schema, every migration and all the queries are shared between backends and authored once; only column types and placeholder syntax are translated. PostgreSQL 13 or newer.

### Configuration

Everything lives in `config.toml` (see `config.example.toml`) and can be overridden per-key with environment variables: `SYNOPTICON_<SECTION>__<KEY>`, e.g. `SYNOPTICON_NAS__URL`. Credentials are best passed env-only. Notable settings:

- `nas.spaces` — `["personal"]` and/or `["shared"]` (Personal vs Shared Space libraries).
- `nas.requests_per_second` — default 4; the NAS also serves your family, be gentle. Writes are throttled separately (1/s).
- `database.backend` — `sqlite` (default, a file under `data_dir`) or `postgres`; see [Database backend](#database-backend).
- `storage.keep_originals` / `storage.originals_cache_gb` — by default originals are evicted after processing under a 50 GB LRU budget; only ~10–30 KB of crops per face are kept. Set `keep_originals = true` (needs roughly your library's size in free disk) to make future detector re-runs NAS-traffic-free.
- `inference.device` — `auto` (default), `cpu`, or `cuda`; see [GPU acceleration](#gpu-acceleration). `inference.device_id` selects the CUDA GPU on multi-GPU hosts.
- `inference.job_threads` / `inference.job_nice` — how much of the machine a job launched from the web GUI may take. By default a job gets `nproc - 1` BLAS/OpenMP threads and runs at niceness 10, which keeps the GUI responsive while it works; see [Jobs and the GUI's responsiveness](#jobs-and-the-guis-responsiveness).
- `[clustering]` / `[crossref]` — the face-grouping tuning surface; change and re-run `synopticon recluster --set clustering.edge_threshold=0.47` cheaply.

### GPU acceleration

Face detection (detect + embed) is the long pass and runs on CPU by default. It will use an NVIDIA GPU via CUDA when two things are true: a CUDA-capable `onnxruntime-gpu` is installed, and `inference.device` is `auto` (the default) or `cuda`. Run `synopticon hwinfo` to see what's detected — it flags the common case of a GPU being present while only the CPU-only `onnxruntime` is installed.

Requirements: just a reasonably recent NVIDIA driver — no system CUDA toolkit. The `gpu` extra is `onnxruntime-gpu[cuda,cudnn]` (CUDA 12 line), whose `[cuda,cudnn]` part pulls NVIDIA's CUDA + cuDNN runtime libraries straight from PyPI as wheels; Synopticon calls `onnxruntime.preload_dlls()` so ONNX Runtime finds them. For Docker you additionally need the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host to expose the driver to the container.

Bare-metal: `uv sync --extra gpu` instead of `--extra cpu`. The two extras are mutually exclusive (`onnxruntime` and `onnxruntime-gpu` share one import namespace), so uv installs exactly one. The `gpu` extra is also incompatible with `restore`/`export` — those pin `torch==2.1.2`, which requires an older cuDNN than the GPU runtime does.

Docker: use the `gpu`-tagged image (or the `gpu` compose profile, which builds the equivalent from source). See [Docker images](#docker-images) for the full tag list.

```bash
docker run --rm --gpus all \
  -v "$PWD/data:/data" -v "$PWD/models:/models" \
  fdebijl/synopticon:gpu extract

# or, building from source:
docker compose --profile gpu build
docker compose --profile gpu run synopticon-gpu extract
```

`device = "auto"` is safe everywhere: if no usable CUDA provider is found — or a CUDA session fails to initialize (missing libraries, GPU out of memory) — Synopticon logs a warning and falls back to CPU rather than aborting the run. Setting `device = "cuda"` does not change this fallback; it only makes the "no GPU" case log a warning.

> Throughput note: this release makes the GPU *usable and robust*, not maximally fast. Detection isn't batched and embedding batches only within a single photo, so GPU utilization is modest; cross-photo batching and CPU/GPU overlap are a planned follow-up.

### Two-factor authentication

If the account has 2FA, set `nas.otp_code` (or `SYNOPTICON_NAS__OTP_CODE`) for the first login; Synopticon stores the returned device token in its database so later logins skip the OTP.

## Models

The face-recognition pipeline needs model weights; deduplication and the housekeeping commands do not. Weights are not bundled (mixed licenses; some upstream terms are research-only — verify they fit your use). `synopticon models download` fetches what it can and prints instructions for the rest:

| Model | Role | Source | Availability |
|---|---|---|---|
| SCRFD-10G-KPS | primary detector | insightface `antelopev2` | auto-download |
| ArcFace R100 (Glint360K) | embedder | insightface `antelopev2` | auto-download |
| YOLOv8-l-face | secondary detector | lindevs/yolov8-face | auto-download |
| AdaFace IR-101 (WebFace12M) | embedder | official checkpoint → `scripts/export_adaface_onnx.py` | optional, manual export |
| MagFace iResNet100 | embedder + quality signal | official checkpoint → `scripts/export_magface_onnx.py` | optional, manual export |

The pipeline degrades gracefully to whatever subset is present (minimum: SCRFD + one embedder). Every model is sha256-verified against `models/manifest.json` on load; the auto-downloaded models are pinned upstream, and locally-exported ones are registered with `models download --allow-record-hash`. Face restoration (CodeFormer) is off by default and lives behind the `restore` extra due to its pinned-torch dependency chain.

### Exporting AdaFace and MagFace

AdaFace and MagFace can't be auto-downloaded — their weights aren't redistributed, so you fetch the official checkpoint yourself and convert it to ONNX with the bundled export scripts. Both scripts take `--checkpoint` and `--out`.

The export scripts depend on `torch==2.1.2` (pinned to match the `restore` extra's constraints), which predates NumPy 2 and cannot interop with the project's `numpy>=2.4.6` pin. So **don't run them via `--extra export` inside the project venv** — the ONNX still traces, but the built-in torch-vs-ONNX numerical check crashes with `Numpy is not available`, leaving the file unverified. Instead run each script in an isolated, project-free env on Python 3.11 with `numpy<2`, so the verification actually runs:

**AdaFace IR-101 (WebFace12M):** grab `adaface_ir101_webface12m.ckpt` from the "Pretrained Models" table in the [mk-minchul/AdaFace](https://github.com/mk-minchul/AdaFace) README (the `IR-101` / `WebFace12M` row), then:

```bash
uv run --no-project --python 3.11 \
  --with "torch==2.1.2" --with "onnx>=1.15" --with "numpy<2" \
  python scripts/export_adaface_onnx.py \
  --checkpoint adaface_ir101_webface12m.ckpt \
  --out models/adaface_ir101_webface12m.onnx
```

**MagFace iResNet100:** grab `magface_iresnet100.pth` from the "Model Zoo" section of the [IrvingMeng/MagFace](https://github.com/IrvingMeng/MagFace) README (the iResNet100 backbone), then:

```bash
uv run --no-project --python 3.11 \
  --with "torch==2.1.2" --with "onnx>=1.15" --with "numpy<2" \
  python scripts/export_magface_onnx.py \
  --checkpoint magface_iresnet100.pth \
  --out models/magface_iresnet100.onnx
```

A successful run prints `torch-vs-ort cosine: 1.000000` and `OK`. The AdaFace export additionally warns about a few unexpected `head.*` keys — that's the training-time margin head, correctly ignored; only the backbone embedding is exported. Once the `.onnx` files exist in `models/`, re-run `synopticon models download --allow-record-hash` to verify and register them in the manifest.

In the web GUI, the same step is the Download models card on the Pipeline page — tick 'record hash' to register manually-copied `.onnx` files — and Settings → Models shows which weights are present and registered.

## How it works

### The face-recognition pipeline

The pipeline runs in a fixed order, with each stage consuming the previous stage's output:

```
sync  →  extract  →  cluster  →  report  →  review  →  apply
(NAS→db) (detect     (group      (static    (approve/  (write
          faces)      faces)     HTML)      reject UI) back to NAS)
└──────── read-only ────────────────────────┘          └─ gated write ─┘
```

(`recluster` and `eval` are side-loops off `cluster`. They group the faces again from the cached embeddings with different parameters and never touch the NAS - useful for benchmarking and testing the best parameters for your system)

1. **`sync`** — pulls your photo list, people, and existing face labels from the NAS (read-only). Existing Synology labels are the base for Synopticon's face grouping.
2. **`extract`** (*Detect faces*) — downloads originals (the originals are not kept on disk to save on space, only small face crops are kept), detects faces with two detectors at multiple scales, and computes up to three embeddings per face into the database. Resumable per photo, so you can run it over multiple hours or days to do the initial catchup run.
3. **`cluster`** (*Group faces*) — builds an exact k-NN similarity graph over all faces, groups them (Chinese Whispers by default), and maps each group onto a Synology person by majority vote. Produces proposals for new face assignments, wrong-label corrections (a face Synology tagged as one person that clearly belongs to another), merge candidates, and new people.
4. **`report` / `review`** — a static HTML report and an interactive web UI to approve or reject each proposal. Nothing is written until you approve it.
5. **`apply`** — pushes approved assignments back via the `add_face` API (works even on photos Synology found no face in). Dry-run by default; merges require a second explicit flag; every write is audit-logged and assignments are individually reversible.
6. **`recluster` / `eval`** — retune thresholds from the embedding cache (no re-detection, no NAS traffic) and measure quality by masking known labels and checking recovery.

### Deduplication

`dedupe` is independent of the face pipeline. Run `sync --hash` at least once to store a sha256 and a 64-bit perceptual hash for every original, then `dedupe` groups byte-identical (`--exact`) and/or visually near-identical (`--visual`) photos. It keeps the best copy of each group (highest resolution, then largest file), and deletes the rest through Synology's background-task API. Like `apply`, it is **dry-run by default**, prints a Synology Photos deep link for every photo it would touch, and audit-logs every deletion.

## Safety model

Synopticon should help you clean up your library, not leave it in a worse than how it found. To that end, every tool that can change your library is opt-in and leaves an audit trail:

- The face pipeline is read-only toward the NAS through `report`/`review`; only `apply`/`apply-all` write.
- `apply` is dry-run by default; `--apply` is required to write, `--apply-merges` is additionally required for merges (a wrong merge is the one hard-to-undo operation — take a NAS snapshot of the Photos database before your first bulk apply), `--apply-merges-named` is separately required for `merge_named` (joining two already-named people destroys a human label — the most dangerous write, so it is never covered by `--apply-merges` and `apply-all` gates it behind its own confirmation), and `--apply-reassigns` is required for reassigns (they alter labels a human can already see in Photos — review these per-item rather than bulk-approving).
- Only reviewer-approved queue items are ever applied; every attempt is recorded in an audit log; assignments are reversible via Synology's `delete_face`, and a reassign is a single `Person.separate` call that can be reversed by moving the face back.
- A face group can propose both a merge of persons A/B and reassigns between them; if the merge is applied first the reassigns become no-ops (the pre-write NAS check skips them).
- `dedupe` follows the same model: dry-run by default, `--apply` (plus a confirmation prompt) required to delete, a per-id idempotency check before each deletion, and every attempt audit-logged. Duplicate deletion is not reversible from Synopticon — confirm your DSM's recycle-bin behavior first.
- [Schedules](#schedules) replay a *saved submission*, not a stored command line: every firing goes back through the same allowlist, parameter whitelist and consent validation a human's click does. Nothing gated behind a typed phrase can be scheduled — the server rejects such a schedule when you try to save it, and the scheduler never supplies a phrase when it fires.
- [QuickMerger](#quickmerger) is the one GUI surface that writes to the NAS outside `apply`. It is interactive by nature (you act on one person at a time), so it confirms once per session rather than per card — but every write needs an explicit consent flag on the API call, a merge re-reads both people from the NAS first and is refused if the merged-away side has a name, and every attempt is audit-logged.
- Recommended first write of any kind: scope narrowly (`--person-id <id>` for a test person, a single known duplicate for `dedupe`) and verify in the Photos UI.

## Compatibility

Works against any NAS running Synology Photos (DSM 7.x). API versions are discovered at runtime (`SYNO.API.Info`), not hardcoded — the Person/Item endpoints vary across DSM releases. The write-back chain (`Person.add_face` v3 + `Upload.Face`) and the deletion chain (`BackgroundTask.File.delete`) were captured against a current DSM; if your DSM predates them, everything read-only still works and the write commands will fail loudly rather than corrupt anything.

## LLM Disclaimer
Parts of this codebase were created by a large language model, in particular models provided by Anthropic.
