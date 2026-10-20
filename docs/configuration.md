# Configuration

Everything lives in `config.toml` (see [`config.example.toml`](../config.example.toml)) and can be overridden per-key with environment variables: `SYNOPTICON_<SECTION>__<KEY>`, e.g. `SYNOPTICON_NAS__URL`. Credentials are best passed env-only. Notable settings:

- `nas.spaces` — `["personal"]` and/or `["shared"]` (Personal vs Shared Space libraries).
- `nas.requests_per_second` — default 4; the NAS also serves your family, be gentle. Writes are throttled separately (1/s).
- `database.backend` — `sqlite` (default, a file under `data_dir`) or `postgres`; see [Database backend](#database-backend).
- `storage.keep_originals` / `storage.originals_cache_gb` — by default originals are evicted after processing under a 50 GB LRU budget; only ~10–30 KB of crops per face are kept. Set `keep_originals = true` (needs roughly your library's size in free disk) to make future detector re-runs NAS-traffic-free.
- `inference.device` — `auto` (default), `cpu`, or `cuda`; see [GPU acceleration](#gpu-acceleration). `inference.device_id` selects the CUDA GPU on multi-GPU hosts.
- `inference.job_threads` / `inference.job_nice` — how much of the machine a job launched from the web GUI may take. By default a job gets `nproc - 1` BLAS/OpenMP threads and runs at niceness 10, which keeps the GUI responsive while it works; see [Jobs and the GUI's responsiveness](adr/06_job-subprocess-execution.md).
- `[clustering]` / `[crossref]` — the face-grouping tuning surface; change and re-run `synopticon recluster --set clustering.edge_threshold=0.47` cheaply.
- `crossref.proposal_kinds` — which suggestion kinds (`assign`, `low_confidence`, `reassign`, `merge`, `merge_named`, `new_person`) Group faces adds to the review queue; all by default. Untick one in Settings → Crossref to stop it being suggested.

## Database backend

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

## GPU acceleration

Face detection (detect + embed) is the long pass and runs on CPU by default. It will use an NVIDIA GPU via CUDA when two things are true: a CUDA-capable `onnxruntime-gpu` is installed, and `inference.device` is `auto` (the default) or `cuda`. Run `synopticon hwinfo` to see what's detected — it flags the common case of a GPU being present while only the CPU-only `onnxruntime` is installed.

Requirements: just a reasonably recent NVIDIA driver — no system CUDA toolkit. The `gpu` extra is `onnxruntime-gpu[cuda,cudnn]` (CUDA 12 line), whose `[cuda,cudnn]` part pulls NVIDIA's CUDA + cuDNN runtime libraries straight from PyPI as wheels; Synopticon calls `onnxruntime.preload_dlls()` so ONNX Runtime finds them. For Docker you additionally need the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host to expose the driver to the container.

Bare-metal: `uv sync --extra gpu` instead of `--extra cpu`. The two extras are mutually exclusive (`onnxruntime` and `onnxruntime-gpu` share one import namespace), so uv installs exactly one. The `gpu` extra is also incompatible with `restore`/`export` — those pin `torch==2.1.2`, which requires an older cuDNN than the GPU runtime does.

Docker: use the `gpu`-tagged image (or the `gpu` compose profile, which builds the equivalent from source). See [Docker images](docker.md#docker-images) for the full tag list.

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

## Two-factor authentication

If the account has 2FA, set `nas.otp_code` (or `SYNOPTICON_NAS__OTP_CODE`) for the first login; Synopticon stores the returned device token in its database so later logins skip the OTP.
