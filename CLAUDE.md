# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Synopticon supplements Synology Photos' face recognition: it syncs a photo library from a NAS, runs an ensemble face pipeline (SCRFD + YOLO detection, ArcFace/AdaFace/MagFace embeddings), clusters faces, cross-references clusters against Synology's existing person labels, and writes approved corrections back through Synology's undocumented Person API. Quality-first, CPU-first (optional CUDA GPU), batch/offline — runtime is explicitly not a constraint; resumability is.

## Commands

```bash
uv sync --extra cpu --extra review --extra faiss   # dev setup (cpu/gpu are mutually exclusive; torch extras are opt-in)
uv run pytest tests/unit/ -q                # full suite (~120 tests, fully mocked, fast)
uv run pytest tests/unit/test_client.py -q  # one file; -k <name> for one test
uv run synopticon --help                    # CLI: check|hwinfo|sync|extract|benchmark|cluster|recluster|reset|report|review|apply|apply-all|dedupe|eval|models
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

Phases before `apply` are read-only toward the NAS. `apply` is dry-run by default (`--apply` to write; `--apply-merges` additionally gates merges — merges are the one irreversible operation; `--apply-reassigns` gates reassigns, which move a face-label a human can already see in Photos). `apply-all` is the one command that writes every approved kind at once with those extra gates implicitly lifted — it still confirms interactively and never dry-runs. Only `review_queue` rows with `status='approved'` are eligible; every write attempt lands in `audit_log`; idempotency pre-checks re-fetch NAS state before each write.

## Conventions

- Python 3.11, `numpy<2` pinned (insightface/basicsr compat).
- New CLI commands go in `cli.py` with lazy imports inside the command function (keeps CLI startup fast), and must also be documented in README.md.
- The README is the FOSS onboarding path — keep its quickstart honest when changing CLI, config, or Docker layout.
- When adding new commands or when changing the data model, do a pass on CLAUDE.md as well to ensure the content is up to date