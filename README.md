# Synopticon

A standalone, quality-first face-recognition supplement for **Synology Photos**. Synology's built-in face detection misses faces and sometimes links photos to the wrong person; Synopticon runs a frontier ensemble pipeline (multi-scale SCRFD + YOLO-face detection, ArcFace + AdaFace + MagFace embeddings, graph clustering) over your library, cross-references the results against what Synology already knows, and writes corrections back through Synology Photos' (undocumented) Person API — every write gated behind your explicit review.

It runs anywhere Docker or Python runs (a homelab box, not the NAS itself), is CPU-only by design, and treats runtime as a non-issue: batch runs of hours or days are expected and fully resumable.

## How it works

1. **`sync`** — pulls your photo list, people, and existing face labels from the NAS (read-only). Existing Synology labels become ground truth.
2. **`extract`** — downloads originals (streamed; only small face crops are kept, originals evicted under a disk budget), detects faces with two detectors at multiple scales, and computes up to three embeddings per face into a local SQLite cache. Interrupt any time; it resumes.
3. **`cluster`** — builds an exact k-NN similarity graph over all faces, clusters (Chinese Whispers by default), and maps clusters onto Synology persons by majority vote. Produces proposals: *new face assignments*, *merge candidates*, and *new people*.
4. **`report` / `review`** — a static HTML report and an interactive web UI to approve or reject each proposal. Nothing is written until you approve it.
5. **`apply`** — pushes approved assignments back via the `add_face` API (works even on photos Synology found no face in). Dry-run by default; merges require a second explicit flag; every write is audit-logged and assignments are individually reversible.
6. **`recluster` / `eval`** — retune thresholds from the embedding cache (no re-extraction, no NAS traffic) and measure quality by masking known labels and checking recovery.

## Quickstart (Docker)

```bash
git clone https://github.com/fdebijl/synopticon && cd synopticon/docker
mkdir -p data models
cp ../config.example.toml data/config.toml   # edit: set [nas] url
export SYNOPTICON_NAS__URL=https://your-nas.example.com
export SYNOPTICON_NAS__ACCOUNT=photos-bot     # a dedicated NAS user is recommended
export SYNOPTICON_NAS__PASSWORD=...

docker compose build
docker compose run synopticon models download
docker compose run synopticon sync
docker compose run synopticon extract          # hours-to-days on CPU; resumable, re-runnable
docker compose run synopticon cluster
docker compose run synopticon report           # static HTML in ./data/report/<run>/
docker compose run --service-ports synopticon review   # http://127.0.0.1:8686
docker compose run synopticon apply            # dry-run; add --apply to write
```

Without Docker: `uv sync`, then `uv run synopticon <command>` (Python 3.11/3.12).

### Configuration

Everything lives in `config.toml` (see `config.example.toml`) and can be overridden per-key with environment variables: `SYNOPTICON_<SECTION>__<KEY>`, e.g. `SYNOPTICON_NAS__URL`. Credentials are best passed env-only. Notable settings:

- `nas.spaces` — `["personal"]` and/or `["shared"]` (Personal vs Shared Space libraries).
- `nas.requests_per_second` — default 4; the NAS also serves your family, be gentle. Writes are throttled separately (1/s).
- `storage.keep_originals` / `storage.originals_cache_gb` — by default originals are evicted after processing under a 50 GB LRU budget; only ~10–30 KB of crops per face are kept. Set `keep_originals = true` (needs roughly your library's size in free disk) to make future detector re-runs NAS-traffic-free.
- `[clustering]` / `[crossref]` — the tuning surface; change and re-run `synopticon recluster --set clustering.edge_threshold=0.47` cheaply.

### Two-factor authentication

If the account has 2FA, set `nas.otp_code` (or `SYNOPTICON_NAS__OTP_CODE`) for the first login; Synopticon stores the returned device token in its database so later logins skip the OTP.

## Models

Weights are **not** bundled (mixed licenses; some upstream terms are research-only — verify they fit your use). `synopticon models download` fetches what it can and prints instructions for the rest:

| Model | Role | Source | Availability |
|---|---|---|---|
| SCRFD-10G-KPS | primary detector | insightface `antelopev2` | auto-download |
| ArcFace R100 (Glint360K) | embedder | insightface `antelopev2` | auto-download |
| YOLOv8-l-face | secondary detector | derronqi/yolov8-face (**AGPL-3.0**) | optional, manual export |
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
uv sync --all-extras --no-extra restore --no-extra export
uv run pytest            # unit tests; fully mocked, never touch a NAS
```

Layout: `syno/` (API client + write-back) · `sync/` (extraction/caching) · `pipeline/` (detect/align/embed) · `cluster/` (graph, Chinese Whispers, cross-reference) · `eval/` (hold-out tuning) · `review/` (report + UI). The `cluster/` package deliberately imports nothing from `syno/`/`pipeline/` — reclustering can never touch the network.

Licensed AGPL-3.0-or-later (the optional YOLOv8-face detector is AGPL; model weights have their own licenses and are never redistributed here).
