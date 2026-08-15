# ADR 03 — Ensemble face pipeline: per-model preprocessing, versioned re-extraction

**Status:** Accepted
**Applies to:** `pipeline/`, `models/manifest.json`, `scripts/download_models.py`

## Context

The pipeline runs an ensemble — SCRFD + YOLO detection, ArcFace/AdaFace/MagFace embeddings — over
a library that can take many hours to process. Three properties follow from that: runs must be
resumable, re-extraction must be triggered only by changes that actually invalidate results, and
a single bad photo must never abort a run.

The models also come from different lineages with genuinely different input conventions, which
looks like inconsistency and invites "cleanup".

## Decision

### Re-extraction is gated by `pipeline_version`

`pipeline_version` is a hash of the model manifest plus the detection config. Per-photo processed
state lives in the `extract_log` table. It is defined in the dependency-free `pipeline/version.py`
(re-exported by `runner`) so the web process can resolve it without importing `cv2` — see ADR 01
and ADR 07.

Each photo is one atomic database transaction, so crash-resume is by construction rather than by
a checkpointing mechanism.

### Embeddings are stored per-model, per-variant

The `embeddings` table is keyed by model and variant. Clustering always uses `variant='orig'`;
restored-face embeddings are advisory only.

### Per-model preprocessing differs deliberately — do not normalize it

Each embedder owns its own preprocessing:

| Model | Preprocessing |
|---|---|
| ArcFace | `(x - 127.5) / 127.5` |
| AdaFace | BGR, `[-1, 1]` |
| MagFace | BGR, plain `x / 255` |

The MagFace convention was verified empirically. The "consistent-looking" alternative — matching
it to the other two — inverts its embedding space. These are not three inconsistent
implementations of one idea; they are three models' actual input contracts.

MagFace's pre-normalization vector magnitude is the per-face quality signal, stored as
`faces.quality`.

### A skipped photo must say why it was skipped

One bad photo never aborts a run. But `str(exc)` alone — "cannot identify image file
'/data/originals/…'" — does not tell a homelabber whether the NAS, the file, or the disk was at
fault.

`runner.skip_reason(exc, filename)` classifies the exception into a plain-language cause. It
matches on class *name* and module, so the runner need not import `syno`, `httpx` or `PIL` to
recognize their exceptions. `_skip_message` renders:

```
skipped photo <id> (<filename>) (space=…): <reason> [<Exc>: <detail>] -> <deep link>
```

to both the logger and the progress emitter, so the web job log carries it. Tracebacks stay at
DEBUG. Reasons are tallied in `ExtractStats.skip_reasons` and printed once as a run-level
breakdown, because the per-photo lines scroll away in a multi-hour run.
`pipeline/crops.py`'s regen skip reuses the same classifier.

### Model weights are never committed or redistributed

`scripts/download_models.py` pins a sha256 per model into `models/manifest.json`, and
`pipeline/manifest.py` refuses to load on mismatch. Pin to versioned release URLs, never
`releases/latest` — the latter silently changes the pipeline version under you.

### Restoration is optional

CodeFormer sits behind the `[restore]` extra with hard-pinned old torch/torchvision, because
basicsr breaks on torchvision ≥ 0.17. Everything must work with it disabled.

## Consequences

- Changing detection config or a model hash re-extracts the whole library. That is intended, and
  it is why the manifest is hashed rather than the weights being trusted by filename.
- The `export`/`restore` extras pin `torch==2.1.2`, which predates NumPy 2 and cannot interop with
  the project's numpy pin. Run the ONNX export scripts in an isolated environment rather than via
  `--extra export`, or their torch-vs-ONNX verification crashes with `Numpy is not available`:

  ```bash
  uv run --no-project --python 3.11 \
    --with "torch==2.1.2" --with "onnx>=1.15" --with "numpy<2" \
    python scripts/export_adaface_onnx.py ...
  ```
