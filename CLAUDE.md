# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Synopticon supplements Synology Photos' face recognition: it syncs a photo library from a NAS, runs an ensemble face pipeline (SCRFD + YOLO detection, ArcFace/AdaFace/MagFace embeddings), clusters faces, cross-references clusters against Synology's existing person labels, and writes approved corrections back through Synology's undocumented Person API. Quality-first, CPU-first (optional CUDA GPU), batch/offline — runtime is explicitly not a constraint; resumability is.

## Commands

```bash
uv sync --extra cpu --extra review --extra faiss   # dev setup (cpu/gpu are mutually exclusive; torch extras are opt-in)
uv run pytest tests/unit/ -q                # full suite (~120 tests, fully mocked, fast)
uv run pytest tests/unit/test_client.py -q  # one file; -k <name> for one test
uv run synopticon --help                    # CLI: check|hwinfo|sync|extract|benchmark|cluster|recluster|reset|clear-queue|delete-crops|regen-crops|report|web|review|apply|apply-all|dedupe|eval|models
uv run --extra review synopticon web         # full web GUI (setup wizard, jobs, review, apply) — needs [review] extra
uv run synopticon check                     # fast read-only NAS connectivity + auth probe
uv run --extra export python scripts/export_adaface_onnx.py ...   # ONNX exports need the export extra
```

The `cpu` and `gpu` extras share one import namespace and are declared conflicting, so `--all-extras` no longer resolves — pick exactly one (`--extra gpu` for CUDA). `uv run` re-syncs the venv to the default extras — torch disappears again after a plain `uv run`. Always use `uv run --extra export` for the export scripts.

Tests must never contact a real NAS; all HTTP is mocked with respx. Live verification is done manually via `synopticon check` / `sync` (read-only).

## Architecture

Pipeline of five CLI phases with **SQLite as the only contract between them** (`data/synopticon.db`; schema in `src/synopticon/db/schema.sql`, migrations appended to `_MIGRATIONS` in `db/store.py`, versioned by `PRAGMA user_version`):

```
syno/ + sync/  ->  pipeline/   ->  cluster/      ->  review/       ->  syno/writeback.py
(NAS API,          (detect,        (kNN graph,       (HTML report,     (apply approved
 sync, download)    align, embed)   CW/HDBSCAN,       FastAPI UI)       items to NAS)
                                    crossref)
```

**Hard module boundary:** `cluster/` must never import from `syno/` or `pipeline/` — reclustering is guaranteed to work offline from cached embeddings. `pipeline/runner.py` takes a `fetch_original(row) -> Path` callable instead of importing `sync/` for the same reason; the CLI (`cli.py`) is where the layers get wired together.

### Synology API layer (`syno/`)

- **Never hardcode API versions.** The live NAS runs newer versions than the reverse-engineered docs once did (e.g. Browse.Item v7, Browse.Person v3 vs the documented v1/v2 — the `SynologyPhotosAPI/` doc repo is no longer checked out here). `client.py` discovers min/max per API at runtime via `SYNO.API.Info` and caches it in `sync_state`.
- Param encoding has Synology-specific quirks centralized in `client.encode_params`: lists/dicts become compact JSON strings (`id=[2660]`), and some string values must be quoted — wrap them in `QuotedString`. Get this wrong and calls fail silently; it's heavily unit-tested.
- Write throttling is auto-detected by method name (`WRITE_METHODS` frozenset in client.py: add_face, merge, set, delete_face, separate, upload, delete) — separate, slower token bucket than reads.
- Write-back payloads are byte-exact from captured browser traffic. Ground truth lives in the sample HAR captures in `./har/` (gitignored beyond the samples): `add_face_to_photo_without_faces.har` (the `Person.add_face` v3 → `Upload.Face.upload` multipart → `list_face` verify chain), `reassign_existing_face_to_other_person.har`, `remove_face_from_photo.har`, and `deleting_one_photo.har` / `deleting_multiple_photos.har` (photo deletion, used by `dedupe`) — consult those before changing payload shapes.
- HAR-verified Person API semantics: `separate` v1 (`face_id=[..]`, `target_id`, quoted `name`) atomically moves an existing face to another person — the face keeps its `face_id` (this is what the `reassign` review kind uses; reversible). `delete_face` v1 (`face_id=[..]`, `person_id`) **hard-deletes** the face detection from the photo — it is not a reversible unassign.
- **Photo deletion is a different API from `delete_face`**: `BackgroundTask.File.delete` v1 (`item_id=[..]` batch, `folder_id=[]`) queues an async background task and returns `task_info` (status `waiting`) — `success:true` means *queued*, not *done* (no status-poll was captured). `dedupe` uses it. Whether it recycle-bins or hard-deletes is not determinable from the captures — verify against the live NAS.
- `SYNO.Foto.*` (personal space) and `SYNO.FotoTeam.*` (shared space) are mirrored APIs; every DB row carries a `space` column and `client.api_name(space, suffix)` picks the namespace.
- **Similar photo groups (stacking)**: a deep link to a non-top-pick member of a Synology "similar photo group" redirects to the homepage — the grouped timeline only surfaces the group's `top_pick` item. `Browse.SimilarItem` (v1–2, both namespaces) is the only source of group membership: `additional=["similar"]` on `Browse.Item.get` is invalid (error 120), and `Browse.Similar` doesn't exist (error 103). `SimilarItem.list` paginates like `Browse.Item.list`; only the top-pick row of each group carries a top-level `similar` key (`{id, count, top_pick, item_id: [...]}`, sibling of `id`/`filename`) — non-top-pick members are omitted from the response entirely. `sync/items.py::sync_similar` records this as `photos.similar_top_pick` (set for every group member including the top pick, NULL when ungrouped); `db/store.py::link_photo_id` resolves a photo id through it, used at every deep-link build site (`review/queries.py`, `cli.py`'s dedupe report, `sync/persons.py`'s face-skip logging) so links always target the visible group cover.

### Content hashes (`sync/hashes.py`)

- `sync --hash` populates `photos.sha256` / `phash` / `hash_cache_key` / `hashed_at` by downloading each original (videos excluded; `live` included). Like the pipeline, it takes a `fetch_original(row) -> Path` callable and commits per photo — resumable by construction.
- **`phash` is a 64-bit DCT pHash (16 hex chars); compare by hamming distance on the bits, never by string equality.** Byte-identical duplicate detection is `sha256`'s job. pHash is computed after EXIF orientation, so a rotated re-import of the same shot hashes identically.
- Re-hashing is gated by `hash_cache_key IS NOT cache_key` (the same skip mechanism as `extract_log`): a photo edited/replaced on the NAS gets picked up on the next `--hash` pass automatically. Regular syncs never touch the hash columns — they're not in `sync_items`' upsert SET list; keep it that way.
- An undecodable image still gets its `sha256` with `phash` NULL, so `phash IS NULL` does not mean "not yet hashed" — check `sha256 IS NULL` for that.

### Deduplication (`dedupe.py` + `dedupe_writeback.py`)

- `dedupe` deletes duplicate photos from the NAS using the stored hashes. Detection (`dedupe.py`) is **NAS-free like `cluster/`** — pure DB reads over `photos`, no `syno`/`pipeline` import; the deletion half (`dedupe_writeback.py`) is the only part that touches the NAS, and the CLI wires them.
- `--exact` groups by `sha256`; `--visual` groups by `phash` hamming distance (`phash_hamming` in `sync/hashes.py`, `int.bit_count()` on XOR) via union-find, so near-duplicates chain transitively. Keep-rule per group: highest `width*height`, then largest `filesize`, then lowest `id` (`_pick_keep`). Both levels can run at once — `collect_drop_ids` de-dups the union so a photo is only deleted once.
- Deletion mirrors `apply`'s safety model: **dry-run by default** (`--apply` to write, with an interactive confirm unless `-y`), a `get_item` idempotency pre-check per id (gone → skip), every attempt audited (`dedupe.delete` / `dryrun.delete`, reusing `apply.log`), and `photos.deleted=1` set locally on success. Orphaned `faces`/`embeddings`/`syno_faces` rows are left as-is — every downstream query filters `deleted=0`.

### Pipeline (`pipeline/`)

- `pipeline_version` (hash of model manifest + detection config) gates re-extraction; per-photo processed state is in the `extract_log` table. Each photo is one atomic DB transaction — crash-resume is by construction.
- Embeddings are stored **per-model, per-variant** in the `embeddings` table; clustering always uses `variant='orig'` (restored-face embeddings are advisory only).
- Per-model preprocessing lives inside each embedder and differs deliberately: ArcFace `(x-127.5)/127.5`, AdaFace BGR `[-1,1]`, **MagFace BGR plain `x/255`** — the MagFace convention was verified empirically (the "consistent-looking" alternative inverts its embedding space). Don't normalize these to match each other.
- MagFace's pre-normalization vector magnitude is the per-face quality signal (`faces.quality`).
- Model weights are never committed or redistributed. `scripts/download_models.py` pins sha256 per model into `models/manifest.json`; `pipeline/manifest.py` refuses to load on mismatch. Pin to versioned release URLs, never `releases/latest`.
- Restoration (CodeFormer) is optional behind the `[restore]` extra with hard-pinned old torch/torchvision (basicsr breaks on torchvision ≥ 0.17); everything must work with it disabled.

### Config (`config.py`)

Precedence: init kwargs > env vars (`SYNOPTICON_<SECTION>__<KEY>`) > `.env` file > TOML. Config file search order: `$SYNOPTICON_CONFIG`, `./config.toml`, `./data/config.toml`, `/data/config.toml`. Storage defaults are repo-root `./data` and `./models`; the Docker image overrides both via env to its volume mounts — same directories on disk either way.

### Safety model (do not weaken)

Phases before `apply` are read-only toward the NAS. `apply` is dry-run by default (`--apply` to write; `--apply-merges` additionally gates ordinary merges — merges are the one irreversible operation; `--apply-reassigns` gates reassigns, which move a face-label a human can already see in Photos). Merges are split by danger into two review kinds: `merge` (at least one side unnamed — the routine case) and `merge_named` (joins two *already-named* people, destroying a human label). `merge_named` is the most dangerous write and is gated by its own `--apply-merges-named`; `--apply-merges` never covers it. Classification happens at generation time in `crossref.run_clustering` (both `persons.name` non-empty → `merge_named`), with migration `0005` reclassifying any pre-existing un-applied both-named `merge` rows. `apply-all` writes every approved kind at once with the merge/reassign gates implicitly lifted — but `merge_named` is the exception: it lists every named↔named pair with a loud warning and requires a *separate* confirmation (or `--apply-merges-named` under `-Y`), so the bulk confirm never sweeps them in. It still confirms interactively and never dry-runs. Only `review_queue` rows with `status='approved'` are eligible; every write attempt lands in `audit_log`; idempotency pre-checks re-fetch NAS state before each write.

### Web GUI (`web/`)

The browser GUI is a lazy-imported FastAPI app behind the `[review]` extra (`tomlkit` now lives in that extra alongside fastapi/uvicorn/jinja2). Everything imports cleanly without the extra — the CLI stays fast; `_require_fastapi()` guards the import. `synopticon web` (`cli.py`) calls `web/app.py::serve`; `synopticon review` is a deprecated thin alias that prints the `/review` URL and delegates to the same `serve`.

- **Module layout:** `app.py` (`create_app(settings, job_manager=None)` — pages, auth middleware, `/api/stats` + `/api/audit`, jobs/review/SSE endpoints, wires the route registrars); `jobs.py` (subprocess `JobManager` + the allowlist/consent layer); `auth.py` (users, sessions, API keys, login rate limiter); `configio.py` (`register_config_routes`: tomlkit round-trip config editing, secret masking); `setup_routes.py` (`register_setup_routes`: wizard status/test-connection via `syno/probe.py`); `ops_routes.py` (`register_ops_routes`); `stats.py` (`gather_stats` — DB-only, NAS-free, degrades to `models_ready:false`/`pipeline_version:null` when weights are absent). `templates/` (Jinja `base` + per-page + `partials/`), `static/` (one `css/app.css`, `js/{lib,job-panel,<page>}.js`). The Dashboard (`templates/index.html.j2` + `static/js/dashboard.js`) renders stat tiles + a sync→extract→cluster→review→apply strip + an audit tail from the embedded `/api/stats` payload; the page route decides the empty-DB CTA server-side.
- **Job allowlist + consent (`jobs.py`, plan §2/§6 — do not weaken):** commands are never raw argv. `JOB_SPECS: dict[str, JobSpec]` maps a job name to a `build_argv(params)` param whitelist + a `DangerLevel` (SAFE/CONFIRM/TYPED_PHRASE). `validate_consent` is the sole place `--apply*`/`-y` flags are appended, gated by the request's `confirm`/gate-boolean/`confirm_phrase`; a missing gate raises `ConsentError` → HTTP 428 (never leaking the phrase). **Hard rule: the GUI must never pass `-Y` or use `apply-all`** — enforced by `_FORBIDDEN_TOKENS = {"apply-all", "-Y"}`, which `resolve_argv`/`submit` refuse. Consent→flag map:

  | Job / form | Consent required | Flags appended |
  |---|---|---|
  | apply dry-run | none (free preview) | *(no `--apply`)* |
  | apply assign/low_confidence/new_person | `confirm` | `--apply` |
  | apply reassign | `confirm` + `apply_reassigns` | `--apply --apply-reassigns` |
  | apply merge | `confirm` + `apply_merges` | `--apply --apply-merges` |
  | apply merge_named | `confirm_phrase == "merge named people"` | `--apply --apply-merges-named` |
  | dedupe `--apply` | `confirm_phrase == "delete duplicates"` | `--apply -y` |
  | reset `--all` | `confirm_phrase == "reset all"` | `-y` |
  | reset / clear-queue / delete-crops | `confirm` | `-y` |

  `recluster` whitelists only `clustering.*`/`crossref.*` override keys (no arbitrary `--set`). `eval` stays CLI-only (absent from `JOB_SPECS`).
- **Progress protocol v1 (`progress.py`, plan §1):** dependency-free leaf module (so `cluster/` may import it without breaching the boundary). `SYNOPTICON_PROGRESS_FILE=<path>` enables a JSONL emitter; unset → cached no-op, terminal output byte-identical. Event kinds: `phase`, `progress` (throttled ≥100 ms, always emits `done==total`), `log`, `result`, `error`; never raises (write errors swallowed). The **process exit code is authoritative** — `JobManager` synthesizes the terminal `final` event from it, `result` is advisory. Job bookkeeping is flat files, no pipeline-DB coupling: `data/jobs/<id>/` holds `events.jsonl`, `stdout.log`/`stderr.log`, and `job.json` (rewritten on state change). One worker thread drains a FIFO (max 5), a 250 ms tailer follows `events.jsonl` into a `seq`-cursored ring buffer; on startup, `running` jobs are re-adopted iff their pid is still a live `synopticon` process (`/proc/<pid>/cmdline` guard), else marked `interrupted`.
- **Auth model (`auth.py` + migration `0006_web_auth.sql`, plan §7):** `web_users` (scrypt password + per-user salt, `hmac.compare_digest`), `web_sessions` (256-bit opaque token stored hashed, HttpOnly+SameSite=Lax cookie, 30-day, `Secure` when the request scheme is https via `--proxy-headers`), `web_api_keys` (`syn_<32hex>`, stored sha256-hashed, named + revocable, sent as `Authorization: Bearer`). Stdlib only — no auth deps. Middleware: unauthenticated page → 302 `/login`, unauthenticated `/api/*` → 401; mutating `/api/*` must be `Content-Type: application/json` (CSRF). **First-boot claim:** with no users, only `/setup`, `/api/setup/*` and `/api/auth/create-account` are reachable; everything else 302s to `/setup` until the single admin account is created.
- **Review page layouts (`templates/review.html.j2` + `static/js/review.js`):** the review queue has two switchable layouts sharing one `#grid` DOM as the single source of truth — the default **Grid**, and **Focus** (a big card deep-cloned from the current grid `.card` + a carousel of thumbs projected from every grid card). `page_review` sanitizes a `view` query param (`focus`|`grid`); the toolbar toggle persists the choice in `localStorage("reviewView")` and the URL (`?view=focus`, dropped for grid). Focus mode re-projects via an `onStateChange` hook and prefetches pages itself (the grid's IntersectionObserver sentinel is `display:none` there). `partials/review_card.html.j2` is the shared card contract — never fork it.
- **`review/queries.py` extraction:** the review-queue data layer (`load_review_items`, `count_review_items`, `decide_item`, `bulk_approve`, `set_suggested_name`, `queue_counts`, `named_merge_pairs`) was pulled out of the old `review/app.py`; `review/app.py` is now a deprecated thin delegate (removed next release). Both the web GUI and `cli.py`'s apply consent previews call `queries.py`.

## Conventions

- Python 3.11, `numpy<2` pinned (insightface/basicsr compat).
- New CLI commands go in `cli.py` with lazy imports inside the command function (keeps CLI startup fast), and must also be documented in README.md.
- The README is the FOSS onboarding path — keep its quickstart honest when changing CLI, config, or Docker layout.
- When adding new commands or when changing the data model, do a pass on CLAUDE.md as well to ensure the content is up to date