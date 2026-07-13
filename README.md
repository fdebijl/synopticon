# Synopticon

A standalone, quality-first face-recognition supplement for **Synology Photos**. Synology's built-in face detection misses faces and sometimes links photos to the wrong person; Synopticon runs a frontier ensemble pipeline (multi-scale SCRFD + YOLO-face detection, ArcFace + AdaFace + MagFace embeddings, graph clustering) over your library, cross-references the results against what Synology already knows, and writes corrections back through Synology Photos' (undocumented) Person API — every write gated behind your explicit review.

It runs anywhere Docker or Python runs (a homelab box, not the NAS itself), runs great on CPU alone but will use an NVIDIA (CUDA) GPU when one is available, and treats runtime as a non-issue: batch runs of hours or days are expected and fully resumable.

## How it works

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
3. **`cluster`** — builds an exact k-NN similarity graph over all faces, clusters (Chinese Whispers by default), and maps clusters onto Synology persons by majority vote. Produces proposals: *new face assignments*, *merge candidates*, and *new people*.
4. **`report` / `review`** — a static HTML report and an interactive web UI to approve or reject each proposal. Nothing is written until you approve it.
5. **`apply`** — pushes approved assignments back via the `add_face` API (works even on photos Synology found no face in). Dry-run by default; merges require a second explicit flag; every write is audit-logged and assignments are individually reversible.
6. **`recluster` / `eval`** — retune thresholds from the embedding cache (no re-extraction, no NAS traffic) and measure quality by masking known labels and checking recovery.

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

**Where state lives:** everything is under the repo root in both flows — `data/` (SQLite db, face crops, reports, originals cache) and `models/` (ONNX weights). Inside the container those directories appear as `/data` and `/models` (the compose file mounts them), but on disk it's the same `./data` and `./models` either way, so you can freely mix Docker and bare-metal runs against the same state.

Without Docker: `uv sync --extra cpu` (or `--extra gpu` for CUDA — see [GPU acceleration](#gpu-acceleration)), then `cp config.example.toml config.toml` (edit `[nas]`; the storage defaults already point at `./data` and `./models`) and `uv run synopticon <command>` (Python 3.11/3.12).

## CLI reference

All commands are subcommands of `synopticon` (Docker: `docker compose run synopticon <command>`). Every command reads `config.toml`; `--space` defaults to all spaces listed in `nas.spaces`. Phases 1–3 are read-only toward the NAS.

### `check`
Verify NAS connectivity and credentials (read-only, fast). No options.

### `hwinfo`
Print hardware/environment stats relevant to face extraction and clustering — CPU model and core count, RAM, ONNX Runtime version and available execution providers (i.e. whether a GPU is usable), detected NVIDIA GPUs, key library versions, which model weights are present, and disk space. Touches nothing and reaches no network; **include its output when filing a bug report.** No options.

### `sync`
Pull photos, persons, and ground-truth labels from the NAS. Prints live progress per phase (an in-place line on a terminal, periodic lines when piped/in Docker logs).

| Option | Default | Description |
|---|---|---|
| `--space TEXT` | all configured | Limit to one space. |
| `--skip-faces` | off | Skip the per-photo face-level ground-truth pass. |
| `--all-faces` | off | Fetch `list_face` for ALL photos, not just those already tagged. |
| `--resume` / `--no-resume` | `--resume` | Resume the faces pass from its saved cursor; `--no-resume` forces a full re-scan. |

The faces pass is checkpointed per-photo, so it resumes cleanly after an interruption. The items and persons passes are idempotent but restart from the top.

### `extract`
Detect faces and compute ensemble embeddings (resumable; interrupt any time). Evicts cached originals afterward unless `storage.keep_originals` is set.

| Option | Default | Description |
|---|---|---|
| `--limit INTEGER` | none | Process at most N photos. |
| `--photo-id INTEGER` | none | Process a single photo id. |
| `--space TEXT` | all configured | Limit to one space. |

### `cluster`
Cluster all embeddings and cross-reference against Synology persons. Writes a new cluster run. No options.

### `recluster`
Re-run clustering from the cached embeddings with parameter overrides. Never hits the NAS.

| Option | Default | Description |
|---|---|---|
| `--set SECTION.KEY=VALUE` | none | Override a config value, e.g. `--set clustering.edge_threshold=0.47`. Repeatable. |

### `reset`
Clear locally-computed data (faces, embeddings, clusters, and the review queue) plus their crop images, so the pipeline can rebuild from scratch. Useful after tweaking detection/clustering settings: the review UI pools items from every run, so stale suggestions would otherwise linger. Synced NAS metadata is kept unless `--all` is given. **Never touches the NAS.**

| Option | Default | Description |
|---|---|---|
| `--all` | off | Also drop synced NAS metadata (photos, persons, ground truth); forces a full re-sync. |
| `--keep-crops` | off | Leave crop images on disk instead of deleting them with the faces. |
| `--yes` / `-y` | off | Skip the confirmation prompt. |

Typical use after changing `[detection]` scores: `synopticon reset` then `synopticon extract && synopticon cluster`.

### `report`
Generate the static HTML review report.

| Option | Default | Description |
|---|---|---|
| `--run-id INTEGER` | latest | Cluster run to report on. |

### `review`
Serve the interactive review UI (approve/reject queue items). Under Docker, add `--service-ports`.

| Option | Default | Description |
|---|---|---|
| `--host TEXT` | `127.0.0.1` | Bind address. |
| `--port INTEGER` | `8686` | Bind port. |

### `apply`
Apply approved review items to the NAS. **Dry-run unless `--apply` is given.**

| Option | Default | Description |
|---|---|---|
| `--kinds TEXT` | `assign` | Comma-separated kinds to apply: `assign`, `merge`, `new_person`. |
| `--person-id INTEGER` | none | Scope to a single person id. |
| `--apply` | off (dry-run) | Actually write to the NAS. |
| `--apply-merges` | off | Extra gate required before any merge is written. |
| `--space TEXT` | `personal` | Space to write to. |
| `--report` | off | Print the audit trail afterward. |

### `models download`
Download and verify model weights into `storage.models_dir`.

| Option | Default | Description |
|---|---|---|
| `--only TEXT` | all | Subset of model keys to fetch. Repeatable. |
| `--allow-record-hash` | off | Record the sha256 of not-yet-pinned (locally exported) models. |

### `eval holdout`
Mask a fraction of known labels, recluster, and measure recovery.

| Option | Default | Description |
|---|---|---|
| `--mask-fraction FLOAT` | `0.2` | Fraction of known labels to mask. |
| `--seed INTEGER` | `42` | Random seed. |

### `eval grid`
Grid-search tunables; writes `eval_grid.csv` under `storage.data_dir`.

| Argument / Option | Default | Description |
|---|---|---|
| `GRID_JSON` (argument) | required | JSON of param→values, e.g. `'{"edge_threshold": [0.45, 0.5, 0.55]}'`. |
| `--mask-fraction FLOAT` | `0.2` | Fraction of known labels to mask. |
| `--seed INTEGER` | `42` | Random seed. |

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

Weights are **not** bundled (mixed licenses; some upstream terms are research-only — verify they fit your use). `synopticon models download` fetches what it can and prints instructions for the rest:

| Model | Role | Source | Availability |
|---|---|---|---|
| SCRFD-10G-KPS | primary detector | insightface `antelopev2` | auto-download |
| ArcFace R100 (Glint360K) | embedder | insightface `antelopev2` | auto-download |
| YOLOv8-l-face | secondary detector | lindevs/yolov8-face (**AGPL-3.0**) | auto-download |
| AdaFace IR-101 (WebFace12M) | embedder | official checkpoint → `scripts/export_adaface_onnx.py` | optional, manual export |
| MagFace iResNet100 | embedder + quality signal | official checkpoint → `scripts/export_magface_onnx.py` | optional, manual export |

The pipeline degrades gracefully to whatever subset is present (minimum: SCRFD + one embedder). Every model is sha256-verified against `models/manifest.json` on load; the auto-downloaded models are pinned upstream, and locally-exported ones are registered with `models download --allow-record-hash`. Face restoration (CodeFormer) is off by default and lives behind the `restore` extra due to its pinned-torch dependency chain.

## Safety model

- Phases 1–3 are **read-only** toward the NAS.
- `apply` is **dry-run by default**; `--apply` is required to write, `--apply-merges` is additionally required for merges (a wrong merge is the one hard-to-undo operation — take a NAS snapshot of the Photos database before your first bulk apply).
- Only reviewer-**approved** queue items are ever applied; every attempt is recorded in an audit log; assignments are reversible via Synology's `delete_face`.
- Recommended first write: scope with `--person-id <id>` to a test person and verify in the Photos UI.

## Compatibility

Works against any NAS running Synology Photos (DSM 7.x). API versions are discovered at runtime (`SYNO.API.Info`), not hardcoded — the Person/Item endpoints vary across DSM releases. The write-back chain (`Person.add_face` v3 + `Upload.Face`) was captured against a current DSM; if your DSM predates it, everything read-only still works and `apply` will fail loudly rather than corrupt anything.

## Development

```bash
uv sync --extra cpu --extra review --extra faiss   # or --extra gpu instead of cpu
uv run pytest            # unit tests; fully mocked, never touch a NAS
```

(`--all-extras` no longer works: the `cpu`/`gpu` extras are mutually exclusive, so pick one explicitly.)

Layout: `syno/` (API client + write-back) · `sync/` (extraction/caching) · `pipeline/` (detect/align/embed) · `cluster/` (graph, Chinese Whispers, cross-reference) · `eval/` (hold-out tuning) · `review/` (report + UI). The `cluster/` package deliberately imports nothing from `syno/`/`pipeline/` — reclustering can never touch the network.

Licensed AGPL-3.0-or-later (the optional YOLOv8-face detector is AGPL; model weights have their own licenses and are never redistributed here).
