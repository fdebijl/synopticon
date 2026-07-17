# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Synopticon supplements Synology Photos' face recognition: it syncs a photo library from a NAS, runs an ensemble face pipeline (SCRFD + YOLO detection, ArcFace/AdaFace/MagFace embeddings), clusters faces, cross-references clusters against Synology's existing person labels, and writes approved corrections back through Synology's undocumented Person API. Quality-first, CPU-only, batch/offline — runtime is explicitly not a constraint; resumability is.

## Commands

```bash
uv sync --all-extras --no-extra restore --no-extra export   # dev setup (torch extras are opt-in)
uv run pytest tests/unit/ -q                # full suite (~70 tests, fully mocked, fast)
uv run pytest tests/unit/test_client.py -q  # one file; -k <name> for one test
uv run synopticon --help                    # CLI: check|sync|extract|cluster|recluster|report|review|apply|eval|models
uv run synopticon check                     # fast read-only NAS connectivity + auth probe
uv run --extra export python scripts/export_adaface_onnx.py ...   # ONNX exports need the export extra
```

`uv run` re-syncs the venv to the default extras — torch disappears again after a plain `uv run`. Always use `uv run --extra export` for the export scripts.

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
- Write throttling is auto-detected by method name (`WRITE_METHODS` frozenset in client.py: add_face, merge, set, delete_face, separate, upload) — separate, slower token bucket than reads.
- Write-back payloads are byte-exact from captured browser traffic. Ground truth lives in the sample HAR captures in `./har/` (gitignored beyond the samples): `add_face_to_photo_without_faces.har` (the `Person.add_face` v3 → `Upload.Face.upload` multipart → `list_face` verify chain), `reassign_existing_face_to_other_person.har`, and `remove_face_from_photo.har` — consult those before changing payload shapes.
- HAR-verified Person API semantics: `separate` v1 (`face_id=[..]`, `target_id`, quoted `name`) atomically moves an existing face to another person — the face keeps its `face_id` (this is what the `reassign` review kind uses; reversible). `delete_face` v1 (`face_id=[..]`, `person_id`) **hard-deletes** the face detection from the photo — it is not a reversible unassign.
- `SYNO.Foto.*` (personal space) and `SYNO.FotoTeam.*` (shared space) are mirrored APIs; every DB row carries a `space` column and `client.api_name(space, suffix)` picks the namespace.

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

Phases before `apply` are read-only toward the NAS. `apply` is dry-run by default (`--apply` to write; `--apply-merges` additionally gates merges — merges are the one irreversible operation). Only `review_queue` rows with `status='approved'` are eligible; every write attempt lands in `audit_log`; idempotency pre-checks re-fetch NAS state before each write.

## Conventions

- Python 3.11, `numpy<2` pinned (insightface/basicsr compat).
- New CLI commands go in `cli.py` with lazy imports inside the command function (keeps CLI startup fast), and must also be documented in README.md.
- The README is the FOSS onboarding path — keep its quickstart honest when changing CLI, config, or Docker layout.
