# Synopticon

A quality-first **toolkit for Synology Photos** — a set of standalone, batch-oriented utilities that run alongside your library and take on the jobs DSM does poorly or doesn't do at all. They share one local SQLite cache, one hardened Synology API layer, and one safety model: **read-only toward the NAS until you explicitly opt into a write**, every write audit-logged and (where possible) reversible, and runtime treated as a non-issue — batch runs of hours or days are expected and fully resumable.

It runs anywhere Docker or Python runs (a homelab box is ideal; running on the NAS itself is not recommended), runs great on CPU alone but will use an NVIDIA (CUDA) GPU when one is available.

## What's in the box

Two tools ship today, with more planned:

- **Face recognition** — Synology's built-in face detection misses faces and sometimes links photos to the wrong person. Synopticon runs a frontier ensemble pipeline (multi-scale SCRFD + YOLO-face detection, ArcFace + AdaFace + MagFace embeddings, graph clustering) over your library, cross-references the results against what Synology already knows, and writes corrections back through Synology Photos' (undocumented) Person API — every write gated behind your explicit review.
- **Deduplication** — finds duplicate photos (byte-identical *or* visually near-identical) from content hashes computed over your originals, keeps the highest-quality copy of each group, and deletes the rest through Synology's background-task API (dry-run by default).

Both build on the same foundation, so new tools slot in without new plumbing: a resumable NAS sync into SQLite, a rate-limited API client that discovers endpoint versions at runtime, and a dry-run-first write path.

## How it works

### The face-recognition pipeline

The pipeline runs in a fixed order, each stage consuming the previous stage's output from the local SQLite cache:

```
sync  →  extract  →  cluster  →  report  →  review  →  apply
(NAS→db) (faces+     (graph +    (static    (approve/  (write
          embeddings) crossref)  HTML)      reject UI) back to NAS)
└──────── read-only ────────────────────────┘          └─ gated write ─┘
```

`recluster` and `eval` are side-loops off `cluster` — they re-run clustering from the cached embeddings with different parameters and never touch the NAS.

1. **`sync`** — pulls your photo list, people, and existing face labels from the NAS (read-only). Existing Synology labels become ground truth.
2. **`extract`** — downloads originals (streamed; only small face crops are kept, originals evicted under a disk budget), detects faces with two detectors at multiple scales, and computes up to three embeddings per face into a local SQLite cache. Interrupt any time; it resumes.
3. **`cluster`** — builds an exact k-NN similarity graph over all faces, clusters (Chinese Whispers by default), and maps clusters onto Synology persons by majority vote. Produces proposals: *new face assignments*, *wrong-label corrections* (a face Synology tagged as one person that clearly belongs to another), *merge candidates*, and *new people*.
4. **`report` / `review`** — a static HTML report and an interactive web UI to approve or reject each proposal. Nothing is written until you approve it.
5. **`apply`** — pushes approved assignments back via the `add_face` API (works even on photos Synology found no face in). Dry-run by default; merges require a second explicit flag; every write is audit-logged and assignments are individually reversible.
6. **`recluster` / `eval`** — retune thresholds from the embedding cache (no re-extraction, no NAS traffic) and measure quality by masking known labels and checking recovery.

### Deduplication

`dedupe` is independent of the face pipeline. Run `sync --hash` once to store a sha256 and a 64-bit perceptual hash for every original, then `dedupe` groups byte-identical (`--exact`) and/or visually near-identical (`--visual`) photos, keeps the best copy of each group (highest resolution, then largest file), and deletes the rest through Synology's background-task API. Like `apply`, it is **dry-run by default**, prints a Synology Photos deep link for every photo it would touch, and audit-logs every deletion.

## Quickstart (Docker)

```bash
git clone https://github.com/fdebijl/synopticon && cd synopticon
mkdir -p data models
cp config.example.toml data/config.toml      # edit: set [nas] url
export SYNOPTICON_NAS__URL=https://your-nas.example.com
export SYNOPTICON_NAS__ACCOUNT=photos-bot     # a dedicated NAS user is recommended
export SYNOPTICON_NAS__PASSWORD=...

docker compose build
docker compose run synopticon models download
docker compose run synopticon sync
docker compose run synopticon extract          # the long pass; resumable, re-runnable (see GPU acceleration below)
docker compose run synopticon cluster
docker compose run synopticon report           # static HTML in ./data/report/<run>/
docker compose run --service-ports synopticon review   # http://127.0.0.1:8686
docker compose run synopticon apply            # dry-run; add --apply to write
```

Deduplication is a separate, shorter flow off the same `sync`:

```bash
docker compose run synopticon sync --hash       # one-time: hash every original (slow, resumable)
docker compose run synopticon dedupe --exact     # dry-run; add --apply to delete
```

**Where state lives:** everything is under the repo root in both flows — `data/` (SQLite db, face crops, reports, originals cache) and `models/` (ONNX weights). Inside the container those directories appear as `/data` and `/models` (the compose file mounts them), but on disk it's the same `./data` and `./models` either way, so you can freely mix Docker and bare-metal runs against the same state.

Without Docker: `uv sync --extra cpu` (or `--extra gpu` for CUDA — see [GPU acceleration](#gpu-acceleration)), then `cp config.example.toml config.toml` (edit `[nas]`; the storage defaults already point at `./data` and `./models`) and `uv run synopticon <command>` (Python 3.11/3.12).

## CLI reference

All commands are subcommands of `synopticon` (Docker: `docker compose run synopticon <command>`). Every command reads `config.toml`; `--space` defaults to all spaces listed in `nas.spaces`. Everything is read-only toward the NAS except `apply`, `apply-all`, and `dedupe --apply`, which are each gated behind an explicit flag.

### Setup & diagnostics

#### `check`
Verify NAS connectivity and credentials (read-only, fast). No options.

#### `hwinfo`
Print hardware/environment stats relevant to face extraction and clustering — CPU model and core count, RAM, ONNX Runtime version and available execution providers (i.e. whether a GPU is usable), detected NVIDIA GPUs, key library versions, which model weights are present, and disk space. Touches nothing and reaches no network; **include its output when filing a bug report.** No options.

#### `models download`
Download and verify model weights into `storage.models_dir`.

| Option | Default | Description |
|---|---|---|
| `--only TEXT` | all | Subset of model keys to fetch. Repeatable. |
| `--allow-record-hash` | off | Record the sha256 of not-yet-pinned (locally exported) models. |

#### `benchmark`
Measure extraction throughput on your hardware without changing anything. Reuses the `extract` pipeline (detect → align → embed) over a sample of photos but writes no faces, embeddings, or crops. Reports photos/sec, faces/sec, ms/photo, and a per-stage breakdown so you can see where time goes. Originals are downloaded and cached exactly as `extract` would. A short warmup pass runs first so one-off ONNX session/thread-pool startup cost doesn't skew the numbers. Pair with `hwinfo` when tuning `[inference]` threads or comparing CPU vs GPU.

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

`--hash` downloads every image original (videos are skipped), so the first pass is slow on a large library; it commits per photo and skips already-hashed photos on re-runs, re-hashing only photos whose Synology `cache_key` changed (i.e. edited/replaced). Cached originals are evicted afterward unless `storage.keep_originals` is set. Compare `phash` values by hamming distance, not equality — byte-identical duplicates are the `sha256` column's job.

### Face recognition

#### `extract`
Detect faces and compute ensemble embeddings (resumable; interrupt any time). Evicts cached originals afterward unless `storage.keep_originals` is set.

| Option | Default | Description |
|---|---|---|
| `--limit INTEGER` | none | Process at most N photos. |
| `--photo-id INTEGER` | none | Process a single photo id. |
| `--space TEXT` | all configured | Limit to one space. |

#### `cluster`
Cluster all embeddings and cross-reference against Synology persons. Writes a new cluster run. No options.

#### `recluster`
Re-run clustering from the cached embeddings with parameter overrides. Never hits the NAS.

| Option | Default | Description |
|---|---|---|
| `--set SECTION.KEY=VALUE` | none | Override a config value, e.g. `--set clustering.edge_threshold=0.47`. Repeatable. |

#### `report`
Generate the static HTML review report.

| Option | Default | Description |
|---|---|---|
| `--run-id INTEGER` | latest | Cluster run to report on. |

#### `review`
Serve the interactive review UI (approve/reject queue items). Under Docker, add `--service-ports`.

| Option | Default | Description |
|---|---|---|
| `--host TEXT` | `127.0.0.1` | Bind address. |
| `--port INTEGER` | `8686` | Bind port. |

#### `apply`
Apply approved review items to the NAS. **Dry-run unless `--apply` is given.**

| Option | Default | Description |
|---|---|---|
| `--kinds TEXT` | `assign,low_confidence` | Comma-separated kinds to apply. `assign` and `low_confidence` are the same reviewer-approved face assignment (they differ only in the pipeline's original confidence), so both apply by default; `merge` is gated by `--apply-merges`; `reassign` (opt-in via `--kinds reassign`) is gated by `--apply-reassigns`. |
| `--person-id INTEGER` | none | Scope to a single person id (matches either side of a merge or reassign). |
| `--apply` | off (dry-run) | Actually write to the NAS. |
| `--apply-merges` | off | Extra gate required before any merge is written. |
| `--apply-reassigns` | off | Extra gate required before any reassign is written — it moves an existing (wrong) Synology face label to a different person via a single reversible `Person.separate` call. |
| `--space TEXT` | `personal` | Space to write to. |
| `--report` | off | Print the audit trail afterward. |

Every run (including dry-runs) appends a per-operation trace to `apply.log` in the project root, one line per writer call — useful for confirming what actually reached the NAS.

#### `apply-all`
Apply **all** approved review items — assigns, low-confidence assigns, reassigns, *and* merges — in one go. Prints a per-kind count of what is about to be written and asks for confirmation; unlike `apply` there is no dry-run stage and the `--apply-merges`/`--apply-reassigns` gates are implicitly lifted. Merges are still irreversible — take a NAS snapshot before a large run.

| Option | Default | Description |
|---|---|---|
| `--yes`, `-Y` | off | Skip the confirmation prompt (for scripted runs). |
| `--person-id INTEGER` | none | Scope to a single person id. |
| `--space TEXT` | `personal` | Space to write to. |
| `--report` | off | Print the audit trail afterward. |

#### `eval holdout`
Mask a fraction of known labels, recluster, and measure recovery.

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
Delete duplicate photos from the NAS using the content hashes `sync --hash` stores. Two independent levels (pass either or both); within each duplicate group the **highest-resolution** photo is kept (tie-break: largest file, then lowest id) and the rest are deleted. Hashes are trusted, so there is no review step — but it is **dry-run by default** and prints a Synology Photos deep link for every photo first, and `--apply` asks for confirmation before writing.

| Option | Default | Description |
|---|---|---|
| `--exact` | off | Delete byte-identical duplicates (grouped by `sha256`). |
| `--visual` | off | Delete visually near-identical duplicates (`phash` within `--threshold` bits; transitively grouped). |
| `--threshold INTEGER` | 5 | Max pHash hamming distance (0–64) for a `--visual` match; lower is stricter. |
| `--apply` | off (dry-run) | Actually delete from the NAS. |
| `--space TEXT` | all configured | Deduplicate within one space (groups never span personal/shared). |
| `--yes`, `-y` | off | Skip the confirmation prompt when deleting. |

Requires a prior `sync --hash` pass to populate the hashes. Each attempt (including dry-runs) is recorded in `audit_log` and `apply.log`. Deletion goes through Synology's `BackgroundTask.File.delete` API as a batched background task; **verify on your DSM whether that moves photos to the recycle bin or hard-deletes them before trusting `--apply` on a large run** — start with one known duplicate and check.

### Housekeeping

These commands manage locally-computed state and crop images. **None of them touch the NAS** (except `regen-crops`, which reads originals but never writes).

#### `reset`
Clear locally-computed data (faces, embeddings, clusters, and the review queue) plus their crop images, so the pipeline can rebuild from scratch. Useful after tweaking detection/clustering settings: the review UI pools items from every run, so stale suggestions would otherwise linger. Synced NAS metadata is kept unless `--all` is given. **Never touches the NAS.**

| Option | Default | Description |
|---|---|---|
| `--all` | off | Also drop synced NAS metadata (photos, persons, ground truth); forces a full re-sync. |
| `--keep-crops` | off | Leave crop images on disk instead of deleting them with the faces. |
| `--yes` / `-y` | off | Skip the confirmation prompt. |

Typical use after changing `[detection]` scores: `synopticon reset` then `synopticon extract && synopticon cluster`.

#### `clear-queue`
Delete only the **pending** review-queue items so the next `cluster` run re-generates them from scratch. Pending rows regenerate cleanly — crossref re-inserts them, picking up any person names synced since the last run (handy when a face was suggested for a person who was still unnamed on the NAS at cluster time). Approved, applied and rejected rows are the ledger crossref uses to avoid re-surfacing work you've already handled, so this command refuses to touch them; use `reset` if you truly want to rebuild everything. **Never touches the NAS.**

| Option | Default | Description |
|---|---|---|
| `--yes` / `-y` | off | Skip the confirmation prompt. |

Typical use after a `sync` fills in previously-missing person names: `synopticon clear-queue` then `synopticon cluster`.

#### `delete-crops`
Delete every face crop image from disk to reclaim space, leaving the `faces` table (bboxes, landmarks, embeddings) untouched. Crops are a pure derived artifact, so this is safe and reversible — rebuild them on demand with `regen-crops`. **Never touches the NAS.**

Crop images can accumulate to many gigabytes over a large library. If you run the pipeline intermittently and don't need the review report/UI between passes, wiping crops after each pass keeps disk usage down — the crops cost nothing to recreate from the cached `faces` rows (plus the originals, re-fetched from the NAS) when you next want to review.

| Option | Default | Description |
|---|---|---|
| `--yes` / `-y` | off | Skip the confirmation prompt. |

#### `regen-crops`
Rebuild face crop images from the stored bboxes/landmarks and the originals (re-fetched from the NAS), without re-running detection or embedding. Use it to recover from a `delete-crops` (or an accidental wipe), or to repair a partial one. Resumable — it commits per photo. Originals are evicted afterward unless `storage.keep_originals` is set. **Reads the NAS but never writes to it.**

| Option | Default | Description |
|---|---|---|
| `--space TEXT` | all | Limit to one space. |
| `--only-missing` / `--all` | `--only-missing` | Only rebuild crops whose files are missing (skips photos already fully on disk, so re-runs are cheap); `--all` rewrites every face's crops. |
| `--limit INTEGER` | none | Process at most N photos. |

### Configuration

Everything lives in `config.toml` (see `config.example.toml`) and can be overridden per-key with environment variables: `SYNOPTICON_<SECTION>__<KEY>`, e.g. `SYNOPTICON_NAS__URL`. Credentials are best passed env-only. Notable settings:

- `nas.spaces` — `["personal"]` and/or `["shared"]` (Personal vs Shared Space libraries).
- `nas.requests_per_second` — default 4; the NAS also serves your family, be gentle. Writes are throttled separately (1/s).
- `storage.keep_originals` / `storage.originals_cache_gb` — by default originals are evicted after processing under a 50 GB LRU budget; only ~10–30 KB of crops per face are kept. Set `keep_originals = true` (needs roughly your library's size in free disk) to make future detector re-runs NAS-traffic-free.
- `inference.device` — `auto` (default), `cpu`, or `cuda`; see [GPU acceleration](#gpu-acceleration). `inference.device_id` selects the CUDA GPU on multi-GPU hosts.
- `[clustering]` / `[crossref]` — the tuning surface; change and re-run `synopticon recluster --set clustering.edge_threshold=0.47` cheaply.

### GPU acceleration

Extraction (detection + embedding) is the long pass and runs on CPU by default. It will use an NVIDIA GPU via CUDA when two things are true: a CUDA-capable `onnxruntime-gpu` is installed, and `inference.device` is `auto` (the default) or `cuda`. Run `synopticon hwinfo` to see what's detected — it flags the common case of a GPU being present while only the CPU-only `onnxruntime` is installed.

**Requirements:** just a reasonably recent NVIDIA driver — no system CUDA toolkit. The `gpu` extra is `onnxruntime-gpu[cuda,cudnn]` (CUDA 12 line), whose `[cuda,cudnn]` part pulls NVIDIA's CUDA + cuDNN runtime libraries straight from PyPI as wheels; Synopticon calls `onnxruntime.preload_dlls()` so ONNX Runtime finds them. For Docker you additionally need the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host to expose the driver to the container.

**Bare-metal:** `uv sync --extra gpu` instead of `--extra cpu`. The two extras are mutually exclusive (`onnxruntime` and `onnxruntime-gpu` share one import namespace), so uv installs exactly one. The `gpu` extra is also incompatible with `restore`/`export` — those pin `torch==2.1.2`, which requires an older cuDNN than the GPU runtime does.

**Docker:**

```bash
docker compose --profile gpu build
docker compose --profile gpu run synopticon-gpu extract
```

`device = "auto"` is safe everywhere: if no usable CUDA provider is found — or a CUDA session fails to initialize (missing libraries, GPU out of memory) — Synopticon logs a warning and falls back to CPU rather than aborting the run. Setting `device = "cuda"` does not change this fallback; it only makes the "no GPU" case log a warning.

> **Throughput note:** this release makes the GPU *usable and robust*, not maximally fast. Detection isn't batched and embedding batches only within a single photo, so GPU utilization is modest; cross-photo batching and CPU/GPU overlap are a planned follow-up.

### Two-factor authentication

If the account has 2FA, set `nas.otp_code` (or `SYNOPTICON_NAS__OTP_CODE`) for the first login; Synopticon stores the returned device token in its database so later logins skip the OTP.

## Models

The face-recognition pipeline needs model weights; deduplication and the housekeeping commands do not. Weights are **not** bundled (mixed licenses; some upstream terms are research-only — verify they fit your use). `synopticon models download` fetches what it can and prints instructions for the rest:

| Model | Role | Source | Availability |
|---|---|---|---|
| SCRFD-10G-KPS | primary detector | insightface `antelopev2` | auto-download |
| ArcFace R100 (Glint360K) | embedder | insightface `antelopev2` | auto-download |
| YOLOv8-l-face | secondary detector | lindevs/yolov8-face (**AGPL-3.0**) | auto-download |
| AdaFace IR-101 (WebFace12M) | embedder | official checkpoint → `scripts/export_adaface_onnx.py` | optional, manual export |
| MagFace iResNet100 | embedder + quality signal | official checkpoint → `scripts/export_magface_onnx.py` | optional, manual export |

The pipeline degrades gracefully to whatever subset is present (minimum: SCRFD + one embedder). Every model is sha256-verified against `models/manifest.json` on load; the auto-downloaded models are pinned upstream, and locally-exported ones are registered with `models download --allow-record-hash`. Face restoration (CodeFormer) is off by default and lives behind the `restore` extra due to its pinned-torch dependency chain.

## Safety model

Every tool that can change your library is opt-in and leaves an audit trail:

- The face pipeline is **read-only** toward the NAS through `report`/`review`; only `apply`/`apply-all` write.
- `apply` is **dry-run by default**; `--apply` is required to write, `--apply-merges` is additionally required for merges (a wrong merge is the one hard-to-undo operation — take a NAS snapshot of the Photos database before your first bulk apply), and `--apply-reassigns` is required for reassigns (they alter labels a human can already see in Photos — review these per-item rather than bulk-approving).
- Only reviewer-**approved** queue items are ever applied; every attempt is recorded in an audit log; assignments are reversible via Synology's `delete_face`, and a reassign is a single `Person.separate` call that can be reversed by moving the face back.
- A cluster can propose both a merge of persons A/B and reassigns between them; if the merge is applied first the reassigns become no-ops (the pre-write NAS check skips them).
- `dedupe` follows the same model: **dry-run by default**, `--apply` (plus a confirmation prompt) required to delete, a per-id idempotency check before each deletion, and every attempt audit-logged. Duplicate deletion is not reversible from Synopticon — confirm your DSM's recycle-bin behavior first.
- Recommended first write of any kind: scope narrowly (`--person-id <id>` for a test person, a single known duplicate for `dedupe`) and verify in the Photos UI.

## Compatibility

Works against any NAS running Synology Photos (DSM 7.x). API versions are discovered at runtime (`SYNO.API.Info`), not hardcoded — the Person/Item endpoints vary across DSM releases. The write-back chain (`Person.add_face` v3 + `Upload.Face`) and the deletion chain (`BackgroundTask.File.delete`) were captured against a current DSM; if your DSM predates them, everything read-only still works and the write commands will fail loudly rather than corrupt anything.

## Development

```bash
uv sync --extra cpu --extra review --extra faiss   # or --extra gpu instead of cpu
uv run pytest            # unit tests; fully mocked, never touch a NAS
```

(`--all-extras` no longer works: the `cpu`/`gpu` extras are mutually exclusive, so pick one explicitly.)

Layout: `syno/` (API client + write-back) · `sync/` (extraction/caching + content hashing) · `pipeline/` (detect/align/embed) · `cluster/` (graph, Chinese Whispers, cross-reference) · `dedupe.py` (hash-based duplicate detection) · `eval/` (hold-out tuning) · `review/` (report + UI). The `cluster/` and `dedupe` layers deliberately import nothing from `syno/`/`pipeline/` — clustering and duplicate *detection* can never touch the network; only the write-back halves do.

Licensed AGPL-3.0-or-later (the optional YOLOv8-face detector is AGPL; model weights have their own licenses and are never redistributed here).
